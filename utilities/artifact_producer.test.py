#!/usr/bin/env python3
"""W7C producer lifecycle tests for `artifact_producer.py`.

Every fixture uses an isolated temporary artifact root and `AGENT_HOME`; the
real canonical root, registry, and routes directory are never touched.
"""
import dataclasses
import importlib.util
import io
import contextlib
import fcntl
import json
import hashlib
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
import artifact_campaign as campaign_reader  # noqa: E402
import artifact_identity as idm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_manifest as m  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_reader as reader  # noqa: E402
import dispatch_contract as D  # noqa: E402

_CORPUS_SPEC = importlib.util.spec_from_file_location(
    "shared_artifact_path_corpus", Path(__file__).with_name("artifact_manifest.test.py"))
_CORPUS = importlib.util.module_from_spec(_CORPUS_SPEC)
_CORPUS_SPEC.loader.exec_module(_CORPUS)

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
        # The cycle is named by the route slug; the campaign by the stream key.
        self.assertEqual((record["slug"], record["title"], record["slug_source"], record["locator_suffix"]),
                         ("w7i-test", "w7i-test", "route", ""))
        self.assertEqual((campaign["slug"], campaign["title"], campaign["slug_source"], campaign["locator_suffix"]),
                         ("w7i-naming", "w7i-naming", "campaign-key", ""))
        self.assertEqual(campaign["locator"], f"{campaign['created_on'][:10]}_w7i-naming")
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
        self.assertEqual((legacy_record["slug"], legacy_record["slug_source"]),
                         ("legacy-goal-for-naming", "derived-legacy-route"))
        self.assertEqual((legacy_campaign["slug"], legacy_campaign["slug_source"]),
                         ("legacy-derived", "campaign-key"))

    def test_campaign_is_named_from_its_key_not_the_first_route_slug(self):
        """TF-Rehancer 2026-09-15: key `tf-rehancer-icassp`, first slug
        `tf-rehancer-analysis-cx` produced campaign folder
        `<date>_tf-rehancer-analysis-cx`."""
        self.activate()
        first_route, first_file = self.route(slug="tf-rehancer-analysis-cx", campaign_key="tf-rehancer-icassp")
        first = P.begin(self.root, route_file=first_file, capability="autopilot-code", intensity="direct")
        campaign = P.read_campaign(self.root, first["campaign_id"])
        date = campaign["created_on"][:10]
        self.assertEqual(campaign["locator"], f"{date}_tf-rehancer-icassp")
        self.assertEqual((campaign["key"], campaign["slug"], campaign["title"], campaign["slug_source"]),
                         ("tf-rehancer-icassp", "tf-rehancer-icassp", "tf-rehancer-icassp", "campaign-key"))
        self.assertEqual(Path(first["cycle_dir"]).name, f"{date}_tf-rehancer-analysis-cx")
        self.assertEqual(Path(first["cycle_dir"]).parent.name, f"{date}_tf-rehancer-icassp")
        # A second route with another slug joins the same folder; it adds a cycle, not a name.
        _, second_file = self.route(slug="tf-rehancer-research", campaign_key="tf-rehancer-icassp",
                                    gate_source="second")
        second = P.begin(self.root, route_file=second_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(second["campaign_id"], first["campaign_id"])
        self.assertEqual(Path(second["cycle_dir"]).parent.name, f"{date}_tf-rehancer-icassp")
        self.assertEqual(P.read_campaign(self.root, first["campaign_id"])["locator"], campaign["locator"])
        # The key is the title verbatim; the locator is its D-88 slug.
        _, odd_file = self.route(slug="bounded-task", campaign_key="TTS.v6:Release", gate_source="odd")
        odd = P.begin(self.root, route_file=odd_file, capability="autopilot-code", intensity="direct")
        odd_campaign = P.read_campaign(self.root, odd["campaign_id"])
        self.assertEqual((odd_campaign["title"], odd_campaign["slug"]), ("TTS.v6:Release", "tts-v6-release"))
        self.assertEqual(odd_campaign["locator"], f"{date}_tts-v6-release")
        # The reserved container keeps its fixed name and stays degraded.
        _, keyless_file = self.route(slug="stray-work", gate_source="keyless")
        keyless = P.begin(self.root, route_file=keyless_file, capability="autopilot-code", intensity="direct")
        container = P.read_campaign(self.root, keyless["campaign_id"])
        self.assertEqual((container["key"], container["slug"], container["title"], container["slug_source"]),
                         ("_unassigned", "unassigned", "_unassigned", "reserved"))
        self.assertTrue(keyless["degraded"])
        summaries = P.list_campaign_summaries(self.root)
        self.assertEqual(sorted(row["key"] for row in summaries),
                         ["TTS.v6:Release", "_unassigned", "tf-rehancer-icassp"])
        by_key = {row["key"]: row for row in summaries}
        self.assertEqual(by_key["tf-rehancer-icassp"]["cycle_count"], 2)
        self.assertTrue(by_key["_unassigned"]["degraded"])
        odd_campaign["state"] = "superseded"
        P._write_campaign(self.root, odd_campaign, exclusive=False)
        self.assertNotIn("TTS.v6:Release", {row["key"] for row in P.list_campaign_summaries(self.root)})
        self.assertIn("TTS.v6:Release", {row["key"] for row in P.list_campaign_summaries(self.root, active_only=False)})

    def test_join_backfills_missing_display_fields_and_repairs_a_promoted_placeholder_title(self):
        self.activate()
        _, first_file = self.route(slug="first-task", campaign_key="promoted-stream")
        first = P.begin(self.root, route_file=first_file, capability="autopilot-code", intensity="direct")
        campaign = P.read_campaign(self.root, first["campaign_id"])
        # A pre-W7I record lacks every display field; the join fills them from the key, not the route.
        legacy = {k: v for k, v in campaign.items() if k not in ("slug", "title", "slug_source", "slug_truncated")}
        P._write_campaign(self.root, legacy, exclusive=False)
        _, second_file = self.route(slug="second-task", campaign_key="promoted-stream", gate_source="second")
        P.begin(self.root, route_file=second_file, capability="autopilot-code", intensity="direct")
        filled = P.read_campaign(self.root, first["campaign_id"])
        self.assertEqual((filled["slug"], filled["title"], filled["slug_source"], filled["slug_truncated"]),
                         ("promoted-stream", "promoted-stream", "campaign-key", False))
        self.assertEqual(filled["locator"], campaign["locator"])
        # A campaign promoted out of `_unassigned` by the metadata amendment still
        # carries the reserved placeholder title; the next join names it by its key
        # so later manifests stop sealing `campaign.title = "_unassigned"`.
        promoted = dict(filled)
        promoted["title"] = "_unassigned"
        P._write_campaign(self.root, promoted, exclusive=False)
        _, third_file = self.route(slug="third-task", campaign_key="promoted-stream", gate_source="third")
        third = P.begin(self.root, route_file=third_file, capability="autopilot-code", intensity="direct")
        repaired = P.read_campaign(self.root, first["campaign_id"])
        self.assertEqual(repaired["title"], "promoted-stream")
        self.assertEqual(repaired["locator"], campaign["locator"])
        self.assertEqual(third["campaign_id"], first["campaign_id"])
        # The reserved container itself is never renamed by a join.
        _, keyless_file = self.route(slug="stray", gate_source="stray")
        keyless = P.begin(self.root, route_file=keyless_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(P.read_campaign(self.root, keyless["campaign_id"])["title"], "_unassigned")

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

    def test_child_seals_before_its_open_parent(self):
        # §45 D-123: the parent is a reference, not an ordering constraint.
        self.activate()
        parent_route, parent_file, parent = self.begin(campaign_key="causal-stream")
        child_route, child_file = self.route(slug="followup", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.write_output(parent)
        self.write_output(child)
        self.close(child_route, child_file)
        self.assertEqual(P.finalize(self.root, cycle_id=child["cycle_id"])["status"], "sealed")
        self.assertTrue((Path(child["cycle_dir"]) / "manifest.json").exists())
        self.assertEqual(P.read_cycle_record(self.root, parent["cycle_id"])["state"], "open")
        self.close(parent_route, parent_file)
        self.assertEqual(P.finalize(self.root, cycle_id=parent["cycle_id"])["status"], "sealed")

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
        # §45 D-123: the parent's state never gates a child; only an unknown parent does.
        parent["state"] = "superseded"
        P._write_cycle_record(self.root, parent, exclusive=False)
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="unknown-parent")[1], capability="autopilot-code",
                    intensity="direct", parent_cycle_id="cyc_" + "9" * 32)
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
        for control in (base / ".cycle.json", base.parent / "campaign.events" / "000001.json",
                        base.parent / "campaign.satisfied.json", base.parent / "campaign.json",
                        base / "manifest.json"):
            with self.subTest(control=control):
                self.assertEqual(P.check_write(self.root, control)["verdict"], "deny")
        for spoof in (base.parent / "campaign.events" / "notes.json",
                      base.parent / "campaign.events" / "nested" / "000002.json",
                      base.parent / "campaign.events" / "nested" / "notes" / "manifest.json"):
            with self.subTest(spoof=spoof):
                self.assertEqual(P.check_write(self.root, spoof)["verdict"], "deny")
                relative = spoof.relative_to(self.root).as_posix()
                classification = m.classify_artifact_path(str(self.root),
                    f"campaigns/{P.read_campaign(self.root, camp)['locator']}",
                    base.relative_to(self.root).as_posix(), "control", relative, "prospective", prospective=True)
                self.assertEqual((classification.allowed, classification.reason),
                                 (False, "campaign-event-path-invalid"))
        self.assertEqual(P.check_write(self.root, self.root / ".runtime" / "spoof" / "manifest.json")["verdict"], "allow")
        ok = P.check_write(self.root, base / "artifacts" / "plans" / "plan.md")
        self.assertEqual((ok["verdict"], ok["reason"], ok["bucket"]), ("allow", "open-cycle-artifacts", "plans"))
        for locator in ("manifest.json", ".cache/manifest.json",
                        "_internal/candidate/round_1/manifest.json",
                        "_internal/candidate/round_2/manifest.json"):
            with self.subTest(locator=locator):
                payload = P.check_write(self.root, base / "artifacts" / locator)
                self.assertEqual((payload["verdict"], payload["reason"]),
                                 ("allow", "open-cycle-artifacts"))
        unknown = P.check_write(self.root, base.parent / "2026-09-04_unknown" / "artifacts" / "x.md")
        self.assertEqual(unknown["reason"], "cycle-unknown")
        self.assertEqual(P.cycle_bucket(self.root, base / "artifacts" / "spec" / "prd.md"), ("spec", cyc))

    def test_shared_classifier_corpus_matches_check_write_namespace(self):
        self.activate()
        _route, _route_file, result = self.begin()
        base = Path(result["cycle_dir"])
        for namespace, relative, kind, prospective, allowed, _reason, _surface_reasons in _CORPUS.SHARED_PATH_CORPUS:
            if namespace not in {"control", "payload"} or kind == "symlink":
                continue
            # check-write owns exact campaign/cycle and payload path decisions;
            # .runtime remains outside this artifact-path gate by approved scope.
            if relative.startswith(".runtime/"):
                continue
            actual = relative.replace("campaigns/camp/cyc", base.relative_to(self.root).as_posix())
            actual = actual.replace("campaigns/camp", base.parent.relative_to(self.root).as_posix())
            target = self.root / actual
            decision = P.check_write(self.root, target)
            expected = "allow" if allowed and namespace == "payload" else "deny"
            if namespace == "control":
                expected = "deny"
            self.assertEqual(decision["verdict"], expected, relative)
        _CORPUS.apply_shared_path_corpus(self)

    def test_sealed_cycle_allows_new_writes(self):
        self.activate()
        route, route_file, result = self.begin()
        target = self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        verdict = P.check_write(self.root, target)
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("allow", "open-cycle-artifacts"))

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

    def test_root_spec_write_denial_names_expected_output_dir(self):
        # Item 7: check_write's own allow/deny judgment is unchanged -- only the
        # denial detail grows an `expected_output_dir` hint, and only when the
        # caller's cycle environment names one. The reason token is immutable
        # (D-86: the fleet cutover gate compares it verbatim).
        self.activate()
        target = self.root / "spec" / "c" / "_internal" / "x.md"
        denial = P.check_write(self.root, target)
        self.assertEqual((denial["verdict"], denial["reason"]), ("deny", "legacy-top-level-write-denied"))
        self.assertNotIn("expected_output_dir", denial)

        route, route_file, result = self.begin()
        output_dir = Path(result["cycle_dir"]) / "artifacts"
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": str(output_dir)}):
            denial_with_hint = P.check_write(self.root, target)
            self.assertEqual(
                (denial_with_hint["verdict"], denial_with_hint["reason"]),
                ("deny", "legacy-top-level-write-denied"),
            )
            self.assertEqual(denial_with_hint["expected_output_dir"], str(output_dir))
            resolved_target = output_dir / "spec" / "c" / "_internal" / "x.md"
            allowed = P.check_write(self.root, resolved_target)
            self.assertEqual(allowed["verdict"], "allow")


class FinalizeTest(ProducerTestBase):
    def test_shared_path_corpus_runs_producer_final_collection(self):
        rows = [row for row in _CORPUS.SHARED_PATH_CORPUS if row[0] in {"payload", "locator"}]
        for namespace, relative, kind, _prospective, allowed, reason, public_reasons in rows:
            with self.subTest(namespace=namespace, path=relative), tempfile.TemporaryDirectory() as scratch:
                cycle_dir = Path(scratch) / "cycle"
                (cycle_dir / "artifacts").mkdir(parents=True)
                if namespace == "payload" and relative.startswith("campaigns/camp/cyc/"):
                    cycle_relative = relative[len("campaigns/camp/cyc/"):]
                else:
                    cycle_relative = relative
                target = cycle_dir / cycle_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == "symlink":
                    outside = Path(scratch) / "outside"
                    outside.write_bytes(b"corpus payload\n")
                    target.symlink_to(outside)
                else:
                    target.write_bytes(b"corpus payload\n")
                excluded, links = [], []
                collected, violations = P._enumerate_output(cycle_dir, excluded=excluded, excluded_symlinks=links)
                effective = os.path.normpath(cycle_relative)
                if kind == "symlink" or (effective.startswith("artifacts/")
                                         and P._outside_inclusion_rule(effective)):
                    # §45 D-123: a link or a hidden/temporary path is left out, not refused.
                    self.assertEqual((violations, collected), ([], []), relative)
                    self.assertIn(effective, links if kind == "symlink" else excluded)
                    continue
                self.assertEqual(not violations, allowed, (relative, violations))
                if allowed:
                    self.assertIn((cycle_relative, b"corpus payload\n"), collected)
                else:
                    typed_reason = violations[0].split(":", 1)[0]
                    expected = public_reasons.get("producer", reason)
                    self.assertEqual(typed_reason, expected, (relative, violations))

    def test_nested_manifest_payload_survives_write_finalize_and_reader(self):
        self.activate()
        route, route_file, result = self.begin()
        data = b'{"ordinary":"payload"}\n'
        locators = (
            "artifacts/manifest.json",
            "artifacts/_internal/candidate/round_1/manifest.json",
            "artifacts/plans/cycle/manifest.json",
        )
        payloads = []
        for locator in locators:
            payload = Path(result["cycle_dir"]) / locator
            admission = P.check_write(self.root, payload)
            self.assertEqual((admission["verdict"], admission["reason"]),
                             ("allow", "open-cycle-artifacts"), locator)
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(data)
            payloads.append(payload)
        self.write_output(result, "plans/cycle/plan.md", b"plan\n")
        self.write_output(result, ".cache/manifest.json", data)  # hidden: written, never listed
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        rows = {row["locator"]["path"]: row for row in document["artifact_revisions"]}
        for locator, payload in zip(locators, payloads):
            row = rows[locator]
            self.assertEqual(payload.read_bytes(), data)
            self.assertEqual(row["content_digest"], m.digest_bytes(data))
            self.assertEqual(row["byte_size"], len(data))
            self.assertNotEqual(row["artifact_id"], document["manifest_id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertNotIn("artifacts/.cache/manifest.json", rows)  # hidden: outside the inclusion rule
        buckets = reader.bucket_dirs(self.root, "plans", include_legacy=False)
        self.assertTrue(any((base / "cycle" / "manifest.json").read_bytes() == data
                            for base, _meta in buckets))

    def test_runtime_spoof_is_not_collected_and_nested_manifest_is_payload(self):
        self.activate()
        route, route_file, result = self.begin()
        cycle_dir = Path(result["cycle_dir"])
        runtime_spoof = self.root / ".runtime" / "spoof" / "manifest.json"
        runtime_spoof.parent.mkdir(parents=True)
        runtime_bytes = b'{"spoof":"runtime"}\n'
        runtime_spoof.write_bytes(runtime_bytes)
        nested = cycle_dir / "artifacts" / "_internal" / "candidate" / "manifest.json"
        nested.parent.mkdir(parents=True)
        nested_bytes = b'{"ordinary":"nested payload"}\n'
        nested.write_bytes(nested_bytes)
        root_identity = L.read_root_identity(self.root).artifact_root_id
        open_record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(open_record["state"], "open")
        self.assertEqual(open_record["cycle_id"], result["cycle_id"])
        self.assertFalse((cycle_dir / "manifest.json").exists())
        self.write_output(result, "plans/cycle/plan.md", b"plan\n")
        self.close(route, route_file)

        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        manifest_path = cycle_dir / "manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = P.read_cycle_record(self.root, result["cycle_id"])
        rows = {row["locator"]["path"]: row for row in document["artifact_revisions"]}
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(Path(sealed["manifest_path"]).resolve(), manifest_path.resolve())
        self.assertEqual(sealed["manifest_digest"], m.manifest_digest(document))
        self.assertEqual(document["cycle"]["cycle_id"], result["cycle_id"])
        self.assertEqual(document["cycle"]["state"], "completed")
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(L.read_root_identity(self.root).artifact_root_id, root_identity)
        self.assertNotIn(".runtime/spoof/manifest.json", rows)
        self.assertIn("artifacts/_internal/candidate/manifest.json", rows)
        self.assertNotEqual(rows["artifacts/_internal/candidate/manifest.json"]["artifact_id"], document["manifest_id"])
        self.assertEqual(rows["artifacts/_internal/candidate/manifest.json"]["content_digest"], m.digest_bytes(nested_bytes))
        self.assertEqual(runtime_spoof.read_bytes(), runtime_bytes)

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

    def test_general_sealed_replay_keeps_the_past_result_when_an_index_row_is_removed(self):
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
        replay = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(replay["cycle_state"], "completed")
        self.assertNotIn(result["cycle_id"], adm.load_index(self.root).manifests)
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

    def test_collection_uses_manifest_paths_before_reading_any_payload(self):
        directory = Path(self._tmp.name) / "collection"
        artifacts = directory / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "a.md").write_text("valid output")
        invalid = artifacts / "bad|name.txt"
        invalid.write_text("retain invalid output")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("premature payload read")):
            rows, violations = P._enumerate_output(directory)
        self.assertEqual(rows, [])
        self.assertIn("locator-invalid-component:artifacts/bad|name.txt", violations)
        invalid.unlink()  # fixture only
        outside = Path(self._tmp.name) / "outside"
        outside.write_text("not a payload")
        link = artifacts / ".link"
        link.symlink_to(outside)
        cache = artifacts / ".cache"
        cache.symlink_to(outside.parent, target_is_directory=True)
        # §45 D-123: a link is lstat-ed and left out, never followed, read or refused.
        links = []
        with mock.patch.object(Path, "read_bytes", lambda path: (_ for _ in ()).throw(
                AssertionError(f"link target read: {path}")) if path.name != "a.md" else b"valid output"):
            rows, violations = P._enumerate_output(directory, excluded_symlinks=links)
        self.assertEqual((rows, violations), ([("artifacts/a.md", b"valid output")], []))
        self.assertEqual(sorted(links), ["artifacts/.cache", "artifacts/.link"])


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

    def test_route_closed_nested_manifest_recovers_before_and_after_commit_idempotently(self):
        self.activate()
        for fault in ("before-manifest", "after-manifest"):
            with self.subTest(fault=fault):
                route, route_file = self.route(slug=f"nested-recovery-{fault}")
                result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", campaign_key=f"nested-recovery-{fault}")
                payload_rel = "_internal/candidate/round_1/manifest.json"
                payload = self.write_output(result, payload_rel, b'{"round":1}\n')
                self.write_output(result, "_internal/candidate/round_2/manifest.json", b'{"round":2}\n')
                self.close(route, route_file)
                if fault == "before-manifest":
                    P._write_journal(self.root, result["cycle_id"], state="sealing",
                                     manifest_digest="sha256:" + "0" * 64,
                                     cycle_path=os.path.relpath(result["cycle_dir"], self.root))
                    recovered = P.recover(self.root)
                    self.assertEqual(recovered["producer"]["rolled_back"], [result["cycle_id"]])
                    self.assertFalse((Path(result["cycle_dir"]) / "manifest.json").exists())
                else:
                    with self.assertRaises(adm.AdmissionRecoveryRequired):
                        P.finalize(self.root, cycle_id=result["cycle_id"], crash_after_manifest=True)
                    recovered = P.recover(self.root)
                    self.assertEqual(recovered["producer"]["rolled_forward"], [result["cycle_id"]])
                sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
                self.assertIn(sealed["status"], {"sealed", "already-sealed"})
                self.assertEqual(payload.read_bytes(), b'{"round":1}\n')
                document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
                rows = {row["locator"]["path"]: row for row in document["artifact_revisions"]}
                for rel in (payload_rel, "_internal/candidate/round_2/manifest.json"):
                    self.assertEqual(rows["artifacts/" + rel]["content_digest"],
                                     m.digest_bytes((Path(result["cycle_dir"]) / "artifacts" / rel).read_bytes()))
                replay = P.recover(self.root)
                self.assertEqual(replay["producer"]["rolled_forward"], [])
                self.assertEqual(P.finalize(self.root, cycle_id=result["cycle_id"])["status"], "already-sealed")

    def test_unrelated_cycle_and_index_identity_snapshot_survives_success_and_faults(self):
        self.activate()
        route, route_file, unrelated = self.begin(campaign_key="unrelated-snapshot")
        protected_payload = self.write_output(unrelated, "plans/protected.md", b"protected bytes\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=unrelated["cycle_id"])
        campaign = P.read_campaign(self.root, unrelated["campaign_id"])
        protected_cycle = Path(unrelated["cycle_dir"])
        protected_files = (
            P.cycle_record_path(self.root, unrelated["cycle_id"]),
            protected_cycle / ".cycle.json", protected_cycle / "manifest.json", protected_payload,
            Path(route_file), adm._root_identity_path(self.root),
            self.root / "campaigns" / campaign["locator"] / "campaign.json",
        )

        def snapshot():
            paths = {str(path): path.read_bytes() for path in protected_files}
            index = json.loads(adm._index_path(self.root).read_text())
            unrelated_rows = sorted((key, row) for key, row in index["manifests"].items()
                                    if row.get("cycle_id") == unrelated["cycle_id"])
            return paths, unrelated_rows, m.digest_bytes(protected_payload.read_bytes())

        before = snapshot()
        for outcome in ("success", "before-manifest", "after-manifest"):
            with self.subTest(outcome=outcome):
                target_route, target_route_file = self.route(slug=f"snapshot-{outcome}")
                target = P.begin(self.root, route_file=target_route_file, capability="autopilot-code",
                                 intensity="direct", campaign_key=f"snapshot-{outcome}")
                self.write_output(target, "_internal/candidate/manifest.json", b"candidate\n")
                self.close(target_route, target_route_file)
                if outcome == "success":
                    P.finalize(self.root, cycle_id=target["cycle_id"])
                elif outcome == "before-manifest":
                    P._write_journal(self.root, target["cycle_id"], state="sealing",
                                     manifest_digest="sha256:" + "0" * 64,
                                     cycle_path=os.path.relpath(target["cycle_dir"], self.root))
                    P.recover(self.root)
                else:
                    with self.assertRaises(adm.AdmissionRecoveryRequired):
                        P.finalize(self.root, cycle_id=target["cycle_id"], crash_after_manifest=True)
                    P.recover(self.root)
                self.assertEqual(snapshot(), before)


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
        # An exact retry reuses its revision without publishing or rewinding latest.
        second = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="prd")
        self.assertFalse(second["reference_created"])
        self.assertEqual(second["shared_reference_id"], admitted["shared_reference_id"])
        self.assertEqual(second["shared_reference_revision_id"], admitted["shared_reference_revision_id"])
        self.assertEqual(json.loads((Path(second["revision_dir"]) / "revision.json").read_text())["sequence"], 1)

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


class CompletedSpecPublicationTest(ProducerTestBase):
    def _completed(self, tree=None, *, capability="autopilot-spec", seal=True):
        self._publication_sequence = getattr(self, "_publication_sequence", 0) + 1
        route, path = self.route("direct", capability,
            "update" if capability == "autopilot-spec" else "academic",
            slug=f"completion-publication-{self._publication_sequence}")
        cycle = P.begin(self.root, route_file=path, capability=capability, intensity="direct")
        for rel, data in (tree or {"prd.md": b"# Official PRD\n"}).items():
            self.write_output(cycle, "spec/" + rel, data)
        if seal:
            self.close(route, path)
            P.finalize(self.root, cycle_id=cycle["cycle_id"])
        return route, path, cycle

    def _seed(self, admission):
        return {"schema_version": 1, "reference_id": admission["shared_reference_id"],
                "revision_id": admission["shared_reference_revision_id"],
                "content_digest": admission["content_digest"]}

    def _publish(self, cycle, *, settle=True):
        return P.completed_spec_publication(self.root, cycle_id=cycle["cycle_id"], settle=settle)

    def test_completed_missing_lineage_is_detected_and_normal_completion_admits_once(self):
        self.activate()
        _, path, cycle = self._completed({"REPORT.md": b"PASS evidence\n", "prd.md": b"# User PRD\n"})
        before = {p: p.read_bytes() for p in Path(cycle["cycle_dir"]).rglob("*") if p.is_file()}
        observed = self._publish(cycle, settle=False)
        self.assertEqual(observed["status"], "pending")
        self.assertIn(observed, P.status(self.root)["shared_spec_publications"])
        self.assertEqual(P.list_references(self.root, "spec"), [])
        first = self._publish(cycle)
        self.assertEqual(first["status"], "admitted", first)
        reference = P.list_references(self.root, "spec")[0]
        self.assertEqual(reference["revisions"], [first["shared_reference_revision_id"]])
        again = self._publish(cycle)
        self.assertEqual(again["admission"]["status"], "reused")
        self.assertEqual(again["shared_reference_revision_id"], first["shared_reference_revision_id"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertTrue(R.outcome_path(path).is_file())

    def test_interrupted_publication_retries_existing_journal_without_rewriting_sealed_work(self):
        self.activate()
        _, path, cycle = self._completed()
        manifest = Path(cycle["cycle_dir"]) / "manifest.json"
        sealed, outcome = manifest.read_bytes(), R.outcome_path(path).read_bytes()
        with mock.patch.object(P, "_commit_shared", side_effect=P.ProducerError("shared-base-mismatch", "fault")):
            pending = self._publish(cycle)
        self.assertEqual(pending["status"], "pending")
        journal = next(P.shared_journal_path(self.root, "probe").parent.glob("*.json"))
        revision_id = json.loads(journal.read_text())["revision_id"]
        recovered = self._publish(cycle)
        self.assertEqual(recovered["status"], "admitted", recovered)
        self.assertEqual(recovered["shared_reference_revision_id"], revision_id)
        self.assertFalse(journal.exists())
        self.assertEqual(manifest.read_bytes(), sealed)
        self.assertEqual(R.outcome_path(path).read_bytes(), outcome)
        self.assertEqual(P.read_cycle_record(self.root, cycle["cycle_id"])["state"], "sealed")

    def test_legacy_public_finalize_recovery_publishes_after_closed_outcome_without_new_command(self):
        import contextlib
        import io
        self.activate()
        _, path, cycle = self._completed()
        manifest = Path(cycle["cycle_dir"]) / "manifest.json"
        original, outcome = manifest.read_bytes(), R.outcome_path(path).read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = P.main(["finalize", "--artifact-root", str(self.root), "--cycle", cycle["cycle_id"]])
        self.assertEqual(code, P.OK, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["shared_publication"]["status"], "admitted")
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(R.outcome_path(path).read_bytes(), outcome)

    def test_actual_component_prd_is_selected_without_using_a_foreign_component_or_snapshot(self):
        self.activate()
        route, _, cycle = self._completed({"prd.md": b"root", "a/prd.md": b"a", "b/prd.md": b"b",
                                          "snapshots/v42/prd.md": b"draft", "REPORT.md": b"PASS"})
        record = P.read_cycle_record(self.root, cycle["cycle_id"])
        directory = Path(cycle["cycle_dir"])
        scoped = {**route, "nodes": [{"write_scope": ["spec/b/**"]}]}
        selected = P.official_spec_primary_path(self.root, record, scoped)
        self.assertEqual(selected, directory / "artifacts/spec/b/prd.md")
        self.assertEqual(selected.read_bytes(), b"b")
        selected.unlink()
        self.assertIsNone(P.official_spec_primary_path(self.root, record, scoped))
        self.assertEqual((directory / "artifacts/spec/a/prd.md").read_bytes(), b"a")

    def test_seed_reference_component_merge_and_later_latest_do_not_rebase_retry(self):
        self.activate()
        _, _, base = self._completed({"a/prd.md": b"# A\nold\n", "b/prd.md": b"# B\nkept\n"})
        initial = self._publish(base)
        seed = self._seed(initial["admission"])
        # A second reference makes a guessed keyless identity ambiguous.
        P.admit_shared(self.root, cycle_id=base["cycle_id"], kind="spec", source="spec",
                       key="separate", allow_new_reference=True)
        receipt = {**seed, "components": ["a"], "seed_complete": True, "component_seeds": {"a": True}}
        _, _, delta = self._completed({"a/prd.md": b"# A\nchanged\n",
                                      P.SPEC_BASE_RECEIPT: json.dumps(receipt).encode()})
        merged = self._publish(delta)
        self.assertEqual(merged["status"], "admitted", merged)
        self.assertEqual(merged["shared_reference_id"], initial["shared_reference_id"])
        self.assertEqual((Path(merged["admission"]["revision_dir"]) / "b/prd.md").read_bytes(), b"# B\nkept\n")
        next_seed = self._seed(merged["admission"])
        _, _, later = self._completed({"a/prd.md": b"# A\nchanged\n", "b/prd.md": b"# B\nnew\n",
                                      P.SPEC_BASE_RECEIPT: json.dumps(next_seed).encode()})
        newest = self._publish(later)
        again = self._publish(delta)
        self.assertEqual(again["shared_reference_revision_id"], merged["shared_reference_revision_id"])
        ref = P._read_json(P._reference_path(self.root, "spec", initial["shared_reference_id"]))
        self.assertEqual(ref["latest_revision_id"], newest["shared_reference_revision_id"])

    def test_competing_latest_conflict_stays_pending_with_original_base_and_bytes(self):
        self.activate()
        _, _, base = self._completed({"prd.md": b"# PRD\n## Policy\nold\n"})
        initial = self._publish(base)["admission"]
        seed = json.dumps(self._seed(initial)).encode()
        _, _, left = self._completed({"prd.md": b"# PRD\n## Policy\nleft\n", P.SPEC_BASE_RECEIPT: seed})
        _, path, right = self._completed({"prd.md": b"# PRD\n## Policy\nright\n", P.SPEC_BASE_RECEIPT: seed})
        winner = self._publish(left)
        before = {p: p.read_bytes() for p in Path(right["cycle_dir"]).rglob("*") if p.is_file()}
        pending = self._publish(right)
        self.assertEqual((pending["status"], pending["reason"]), ("pending", "shared-spec-conflict"))
        self.assertEqual(self._publish(right)["reason"], "shared-spec-conflict")
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        ref = P._read_json(P._reference_path(self.root, "spec", initial["shared_reference_id"]))
        self.assertEqual(ref["latest_revision_id"], winner["shared_reference_revision_id"])
        self.assertTrue(R.outcome_path(path).is_file())

    def test_missing_prd_draft_abandoned_and_research_are_not_promoted(self):
        self.activate()
        _, _, missing = self._completed({"REPORT.md": b"PASS\n"})
        self.assertEqual(self._publish(missing)["reason"], "official-spec-prd-missing")
        _, _, draft = self._completed(seal=False)
        self.assertEqual(self._publish(draft)["status"], "not-applicable")
        _, _, research = self._completed(capability="autopilot-research")
        self.assertEqual(self._publish(research)["status"], "not-applicable")
        P.finalize(self.root, cycle_id=draft["cycle_id"], allow_open_route=True,
                   state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(self._publish(draft)["status"], "not-applicable")
        self.assertEqual(P.list_references(self.root, "spec"), [])


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

    def test_finalize_allow_open_route_cli_prints_provisional_warning_to_stderr(self):
        import io
        import contextlib
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = P.main(["finalize", "--artifact-root", str(self.root), "--cycle", cycle_id,
                           "--allow-open-route"])
        self.assertEqual(code, P.OK)
        self.assertEqual(json.loads(out.getvalue().strip().splitlines()[-1])["provisional"], True)
        self.assertIn("cannot make this cycle completed", err.getvalue())

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

    def test_sealed_on_disk_cycle_takes_further_writes(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "sealed")
        target = Path(result["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "extra.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "allow")
        self.assertEqual(verdict["reason"], "open-cycle-artifacts")

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

    def _complete_inline_for_latch(self, route, evidence):
        node = next(item for item in route["nodes"] if item.get("terminal"))
        R.write_completion_marker(route, node, node["id"], evidence, jobs=self.jobs)
        gates = R.terminal_gate_observation(route, jobs=self.jobs)
        self.assertTrue(R.terminal_gate_proven(gates), gates)

    def test_exact_diagnostic_fixture_false_close_then_complete_is_consumed(self):
        self.activate()
        route, route_file, result = self.begin()
        evidence = self.write_output(result)
        cycle_id = result["cycle_id"]
        initial, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created)
        self.assertFalse(initial["terminal_gate_proven"])
        original = R.outcome_path(route_file).read_bytes()
        self._complete_inline_for_latch(route, evidence)
        completed_outcome, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created)
        self.assertTrue(completed_outcome["terminal_gate_proven"])
        retained = list(Path(route_file).parent.glob(
            f"{Path(route_file).stem}.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)
        sealed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(sealed["cycle_state"], "completed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(document["cycle"]["state"], "completed")
        self.assertTrue(document["routes"][0]["terminal_marker"].startswith("sha256:"))

    def test_provisional_active_payload_unchanged_finalize_publishes_completed_revision(self):
        self.activate()
        route, route_file, result = self.begin()
        evidence = self.write_output(result)
        cycle_id = result["cycle_id"]
        initial = P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        self.assertEqual(initial["cycle_state"], "active")
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest_path.read_bytes()
        first, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created)
        self.assertFalse(first["terminal_gate_proven"])
        self._complete_inline_for_latch(route, evidence)
        promoted, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created)
        self.assertTrue(promoted["terminal_gate_proven"])
        result = P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(result["cycle_state"], "completed")
        after = manifest_path.read_bytes()
        self.assertNotEqual(before, after)
        current = json.loads(after)
        prior = json.loads(before)
        self.assertEqual(current["cycle"]["state"], "completed")
        self.assertTrue(m.validate_update(current, previous=prior).ok)
        foreign = json.loads(json.dumps(current))
        foreign["routes"][0]["route_hash"] = "sha256:" + "0" * 64
        self.assertFalse(m.validate_update(foreign, previous=prior).ok)
        self.assertEqual(len([row for row in current["events"] if row["event_type"] == "cycle.completed"]), 1)
        self.assertEqual(len([row for row in current["events"] if row["event_type"] == "route.terminal.recorded"]), 1)
        index = adm.load_index(self.root)
        self.assertEqual(index.manifests[cycle_id]["manifest_digest"], m.manifest_digest(current))
        self.assertEqual(index.cycles[cycle_id]["manifest_digest"], m.manifest_digest(current))

    def test_provisional_active_without_exact_completion_stays_active_and_retry_is_once(self):
        self.activate()
        route, route_file, result = self.begin()
        evidence = self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest_path.read_bytes()
        R.close_route(route, route_file, jobs=self.jobs)  # unproven close: no terminal marker yet
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        refreshed = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        self.assertNotEqual(refreshed.get("status"), "emitted", refreshed)
        self.assertEqual(manifest_path.read_bytes(), before)
        self._complete_inline_for_latch(route, evidence)
        R.close_route(route, route_file, jobs=self.jobs)
        P.finalize(self.root, cycle_id=cycle_id, state="completed")
        completed = manifest_path.read_bytes()
        again = P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(again["cycle_state"], "completed")
        self.assertEqual(manifest_path.read_bytes(), completed)
        document = json.loads(completed)
        self.assertEqual(len([r for r in document["events"] if r["event_type"] == "cycle.completed"]), 1)
        self.assertEqual(len([r for r in document["events"] if r["event_type"] == "route.terminal.recorded"]), 1)

    def test_provisional_refresh_payload_unchanged_publishes_completed_revision(self):
        self.activate()
        route, route_file, result = self.begin()
        evidence = self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        before = (Path(result["cycle_dir"]) / "manifest.json").read_bytes()
        R.close_route(route, route_file, jobs=self.jobs)
        self._complete_inline_for_latch(route, evidence)
        closed, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created); self.assertTrue(closed["terminal_gate_proven"])
        refreshed = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        self.assertEqual(refreshed["status"], "emitted", refreshed)
        current = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(current["cycle"]["state"], "completed")
        index = adm.load_index(self.root)
        self.assertEqual(index.manifests[cycle_id]["manifest_digest"], m.manifest_digest(current))
        status = campaign_reader.status(self.root, result["campaign_id"])
        cycle = next(row for row in status["cycles"] if row["cycle_id"] == cycle_id)
        self.assertEqual(cycle["state"], "completed")
        self.assertEqual(cycle["disposition"], "completed")
        self.assertNotEqual(before, (Path(result["cycle_dir"]) / "manifest.json").read_bytes())

    def test_late_marker_does_not_promote_explicitly_abandoned_cycle_or_outcome(self):
        self.activate()
        route, route_file, result = self.begin()
        evidence = self.write_output(result)
        first, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
        raw = R.outcome_path(route_file).read_bytes()
        P.finalize(self.root, cycle_id=result["cycle_id"], state="abandoned",
                   abandon_reason="operator-decision")
        self._complete_inline_for_latch(route, evidence)
        again, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertFalse(created); self.assertFalse(again["terminal_gate_proven"])
        self.assertEqual(R.outcome_path(route_file).read_bytes(), raw)
        manifest = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(manifest["cycle"]["state"], "abandoned")

    def _provisional_active_with_proven_close(self, extra_payload=False):
        self.activate()
        route, route_file, result = self.begin()
        evidence = self.write_output(result)
        if extra_payload:  # the terminal evidence itself must stay as the marker recorded it
            self.write_output(result, rel="notes/extra.md", data=b"extra body\n")
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        R.close_route(route, route_file, jobs=self.jobs)
        self._complete_inline_for_latch(route, evidence)
        closed, created = R.close_route(route, route_file, jobs=self.jobs)
        self.assertTrue(created and closed["terminal_gate_proven"])
        return route, route_file, result, cycle_id, manifest_path

    def _state_snapshot(self, cycle_id, manifest_path):
        record = P.read_cycle_record(self.root, cycle_id)
        index = adm.load_index(self.root)
        return (manifest_path.read_bytes(), json.dumps(record, sort_keys=True),
                json.dumps(index.manifests[cycle_id], sort_keys=True), P.journal_path(self.root, cycle_id).exists())

    def _cancel_workflow_for_latch(self, route):
        import workflow_state
        ledger = workflow_state.WorkflowLedger(route["route_id"], route["route_hash"], jobs=self.jobs)
        for state in ("READY", "RUNNING", "CANCELLED"):
            ledger.set_workflow_state(state, actor="fixture", evidence={"reason": "user stop"})
        return ledger

    def test_actual_cancelled_workflow_keeps_provisional_finalize_and_refresh_active(self):
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close()
        ledger = self._cancel_workflow_for_latch(route)
        ledger.state_path.write_text('{"workflow_state":"RUNNING"}\n', encoding="utf-8")
        journal, cache = ledger.journal_path.read_bytes(), ledger.state_path.read_bytes()
        before = self._state_snapshot(cycle_id, manifest_path)
        proven = R.outcome_path(route_file).read_bytes()
        self.assertNotIn("workflow_state", route)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        self.assertEqual(self._state_snapshot(cycle_id, manifest_path), before)
        refreshed = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        self.assertNotEqual(refreshed.get("status"), "emitted", refreshed)
        self.assertEqual(self._state_snapshot(cycle_id, manifest_path), before)
        self.assertEqual(R.outcome_path(route_file).read_bytes(), proven)
        self.assertEqual(ledger.read_only_state()["workflow_state"], "CANCELLED")
        self.assertEqual(ledger.journal_path.read_bytes(), journal)
        self.assertEqual(ledger.state_path.read_bytes(), cache)

    def test_provisional_active_payload_edit_and_completion_land_in_one_revision(self):
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close(
            extra_payload=True)
        prior = json.loads(manifest_path.read_text())
        output = Path(result["cycle_dir"]) / "artifacts" / "notes/extra.md"
        output.write_bytes(b"extra body, edited before the late proof is consumed\n")
        refreshed = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        self.assertEqual(refreshed["status"], "emitted", refreshed)
        self.assertEqual(refreshed["changes"]["modified"], ["artifacts/notes/extra.md"])
        current = json.loads(manifest_path.read_text())
        self.assertEqual(current["cycle"]["state"], "completed")
        self.assertNotEqual(current["manifest_revision_id"], prior["manifest_revision_id"])
        digests = {row["content_digest"] for row in current["artifact_revisions"]}
        self.assertIn("sha256:" + hashlib.sha256(output.read_bytes()).hexdigest(), digests)
        new_events = [row["event_type"] for row in current["events"][len(prior["events"]):]]
        self.assertEqual(sorted(new_events), ["artifact.revision.recorded", "cycle.completed", "route.terminal.recorded"])
        self.assertTrue(m.validate_update(current, previous=prior).ok)
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m.manifest_digest(current))
        # the proven outcome is a recorded fact: a later edit does not turn it back
        output.write_bytes(b"edited again\n")
        later = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        self.assertNotEqual(later.get("status"), "skipped", later)
        self.assertEqual(json.loads(manifest_path.read_text())["cycle"]["state"], "completed")

    def _assert_completion_crash_rolls_forward_once(self, publish):
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close()
        before = manifest_path.read_bytes()
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            publish(cycle_id)
        crashed = manifest_path.read_bytes()
        self.assertNotEqual(crashed, before)  # the commit point passed; the index still names the earlier one
        self.assertEqual(P.status(self.root)["pending_journals"], [cycle_id])
        P.recover(self.root)
        self.assertEqual(manifest_path.read_bytes(), crashed)
        current = json.loads(crashed)
        self.assertEqual(current["cycle"]["state"], "completed")
        self.assertEqual(len([r for r in current["events"] if r["event_type"] == "cycle.completed"]), 1)
        self.assertEqual(len([r for r in current["events"] if r["event_type"] == "route.terminal.recorded"]), 1)
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m.manifest_digest(current))
        self.assertEqual(P.status(self.root)["pending_journals"], [])
        again = P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(again["cycle_state"], "completed")
        self.assertEqual(manifest_path.read_bytes(), crashed)

    def test_provisional_completion_by_finalize_crash_after_manifest_rolls_forward_once(self):
        self._assert_completion_crash_rolls_forward_once(
            lambda cycle_id: P.finalize(self.root, cycle_id=cycle_id, state="completed", crash_after_manifest=True))

    def test_provisional_completion_by_refresh_crash_after_manifest_rolls_forward_once(self):
        def refresh(cycle_id):
            result = P.refresh_cycle(self.root, cycle_id, trigger="explicit", crash_after_manifest=True)
            self.fail(result)  # refresh reports a crash as AdmissionRecoveryRequired, not as a result
        self._assert_completion_crash_rolls_forward_once(refresh)

    def test_concurrent_finalize_and_refresh_complete_a_provisional_active_cycle_once(self):
        import concurrent.futures
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close()
        calls = [lambda: P.finalize(self.root, cycle_id=cycle_id, state="completed"),
                 lambda: P.refresh_cycle(self.root, cycle_id, trigger="explicit"),
                 lambda: P.finalize(self.root, cycle_id=cycle_id, state="completed")]
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = []
            for future in [pool.submit(call) for call in calls]:
                try:
                    outcomes.append(future.result(timeout=120))
                except P.ProducerError as exc:  # a loser that found it already settled may only conflict cleanly
                    outcomes.append(exc)
        current = json.loads(manifest_path.read_text())
        self.assertEqual(current["cycle"]["state"], "completed", outcomes)
        self.assertEqual(len([r for r in current["events"] if r["event_type"] == "cycle.completed"]), 1)
        self.assertEqual(len([r for r in current["events"] if r["event_type"] == "route.terminal.recorded"]), 1)
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m.manifest_digest(current))
        self.assertEqual(P.status(self.root)["pending_journals"], [])
        settled = manifest_path.read_bytes()
        P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(manifest_path.read_bytes(), settled)

    def _assert_stale_completion_candidate_is_superseded(self, injection):
        # The candidate is decided from observations taken before the admission lock; a review
        # lease (or an outcome turned abandoned) that appears right after it keeps the document.
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close()
        before = self._state_snapshot(cycle_id, manifest_path)
        original = P._provisional_completion_projection
        patches = []

        def inject(*args, **kwargs):
            candidate = original(*args, **kwargs)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["cycle"]["state"], "completed")
            if injection == "lease":
                patch = mock.patch.object(P, "_live_review_lease", return_value={"lease": "live"})
                patch.start(); patches.append(patch)
            elif injection == "cancelled-workflow":
                self._cancel_workflow_for_latch(route)
            else:
                outcome = json.loads(R.outcome_path(route_file).read_text())
                outcome["disposition"] = "abandoned"
                R.outcome_path(route_file).write_text(json.dumps(outcome))
            return candidate

        try:
            with mock.patch.object(P, "_provisional_completion_projection", inject):
                refreshed = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(refreshed["status"], "skipped", refreshed)
        self.assertEqual(refreshed["reason"], "superseded", refreshed)
        self.assertEqual(self._state_snapshot(cycle_id, manifest_path), before)
        self.assertEqual(json.loads(manifest_path.read_text())["cycle"]["state"], "active")

    def test_observed_refresh_completion_candidate_with_lease_after_candidate_is_superseded(self):
        self._assert_stale_completion_candidate_is_superseded("lease")

    def test_observed_refresh_completion_candidate_with_abandoned_outcome_after_candidate_is_superseded(self):
        self._assert_stale_completion_candidate_is_superseded("abandoned-outcome")

    def test_observed_refresh_completion_candidate_with_cancelled_workflow_is_superseded(self):
        self._assert_stale_completion_candidate_is_superseded("cancelled-workflow")

    def _assert_first_completion_negative_keeps_active(self, mutate):
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close()
        output = Path(result["cycle_dir"]) / "artifacts" / "plans/cycle/plan.md"
        undo = mutate(output)
        before = self._state_snapshot(cycle_id, manifest_path)
        try:
            refreshed = P.refresh_cycle(self.root, cycle_id, trigger="explicit")
            self.assertNotEqual(refreshed.get("status"), "emitted", refreshed)
            with self.assertRaises(P.ProducerError) as caught:
                P.finalize(self.root, cycle_id=cycle_id, state="completed")
            self.assertEqual(caught.exception.code, "finalize-state-conflict")
        finally:
            if undo:
                undo()
        self.assertEqual(self._state_snapshot(cycle_id, manifest_path), before)
        self.assertEqual(json.loads(manifest_path.read_text())["cycle"]["state"], "active")

    def test_first_completion_with_active_review_lease_keeps_provisional_active(self):
        patch = mock.patch.object(P, "_live_review_lease", return_value={"lease": "live"})
        self._assert_first_completion_negative_keeps_active(lambda output: (patch.start(), patch.stop)[1])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-0 file")
    def test_first_completion_with_unreadable_payload_keeps_provisional_active(self):
        def mutate(output):
            output.chmod(0)
            return lambda: output.chmod(0o644)
        self._assert_first_completion_negative_keeps_active(mutate)

    def test_first_completion_with_nonregular_payload_keeps_provisional_active(self):
        def mutate(output):
            data = output.read_bytes(); output.unlink(); output.mkdir()
            def undo():
                output.rmdir(); output.write_bytes(data)
            return undo
        self._assert_first_completion_negative_keeps_active(mutate)

    def test_first_completion_with_missing_required_payload_keeps_provisional_active(self):
        # The removal itself is an ordinary payload refresh; the cycle it leaves without its
        # required `primary` revision is not completed by the late terminal proof.
        route, route_file, result, cycle_id, manifest_path = self._provisional_active_with_proven_close()
        (Path(result["cycle_dir"]) / "artifacts" / "plans/cycle/plan.md").unlink()
        P.refresh_cycle(self.root, cycle_id, trigger="explicit")
        document = json.loads(manifest_path.read_text())
        self.assertEqual(document["cycle"]["state"], "active")
        self.assertEqual(document["cycle"]["outcome_criterion"]["required_artifact_roles"], ["primary"])
        self.assertEqual([row for row in document["events"] if row["event_type"] == "cycle.completed"], [])
        before = self._state_snapshot(cycle_id, manifest_path)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        self.assertEqual(self._state_snapshot(cycle_id, manifest_path), before)

    def test_allow_open_first_publication_succeeds_but_identical_retry_now_conflicts(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        sealed = P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["cycle_state"], "active")
        self.assertIs(sealed["provisional"], True)
        self.assertIn("cannot make this cycle completed", sealed["warning"])
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed", allow_open_route=True)
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        self.assertIn("requested=completed", caught.exception.detail)
        self.assertIn("published_cycle_state=active", caught.exception.detail)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)

    def test_allow_open_retry_after_exact_route_close_promotes(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        self.close(route, route_file)
        completed = P.finalize(self.root, cycle_id=cycle_id, state="completed", allow_open_route=True)
        self.assertEqual(completed["cycle_state"], "completed")
        self.assertNotEqual(manifest_path.read_bytes(), manifest_bytes)
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(record["cycle_state"], "completed")

    def test_completed_snapshot_refuses_abandon_and_abandoned_refuses_completed(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        completed_sealed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertNotIn("provisional", completed_sealed)
        self.assertNotIn("warning", completed_sealed)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

        route2, route_file2 = self.route(gate_source="fixture-2")
        result2 = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        self.write_output(result2)
        R.close_route(route2, route_file2, commit="a" * 40, summary="abandoned fixture")
        cycle_id2 = result2["cycle_id"]
        abandoned_sealed = P.finalize(self.root, cycle_id=cycle_id2, state="abandoned", abandon_reason="operator-decision")
        self.assertNotIn("provisional", abandoned_sealed)
        self.assertNotIn("warning", abandoned_sealed)
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
        # Ordinary edits leave automatic observation history; the past result
        # and identity still agree, and malformed bytes never grant completion.
        observed = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual({k: v for k, v in observed.items() if k not in {"control_observations", "control_baselines"}},
                         json.loads(before["record"]))
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
        self.assertEqual(superseded_first["disposition"]["kind"], "superseded")
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
        record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertEqual(P.cycle_disposition(record)["kind"], "superseded")
        self.assertEqual(record["state"], "sealed")
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
        reference = P.find_reference_by_key(self.root, "spec", "prd")
        kw.setdefault("base_revision", reference["latest_revision_id"] if reference else "none")
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


class SharedBaseGuardTest(ProducerTestBase):
    _cycle = ComponentSetPreservation._cycle
    _reference = ComponentSetPreservation._reference
    _journals = ComponentSetPreservation._journals

    def _admit(self, result, generation, **kw):
        return P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec",
                              source=f"gen{generation}", key="prd", **kw)

    def test_same_base_second_writer_refused_without_any_admission_residue(self):
        cycle = self._cycle([["a"], ["a"], ["a"]])
        first = self._admit(cycle, 0)
        base = first["shared_reference_revision_id"]
        winner = self._admit(cycle, 1, base_revision=base)
        before = self._reference(first["shared_reference_id"])
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(cycle, 2, base_revision=base)
        self.assertEqual(ctx.exception.code, "shared-spec-conflict")
        self.assertEqual(self._reference(first["shared_reference_id"]), before)
        self.assertEqual(self._journals(), [])
        retry = self._admit(cycle, 0)
        self.assertEqual(retry["status"], "reused")
        self.assertEqual(self._reference(first["shared_reference_id"])["latest_revision_id"],
                         winner["shared_reference_revision_id"])

    def test_existing_reference_needs_an_explicit_or_sealed_base(self):
        cycle = self._cycle([["a"], ["a"]])
        self._admit(cycle, 0)
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(cycle, 1)
        self.assertEqual(ctx.exception.code, "shared-base-required")
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(cycle, 1, base_revision="none")
        self.assertEqual(ctx.exception.code, "shared-base-mismatch")

    def test_payload_edit_after_sealing_is_published_as_it_is_now_and_receipt_must_stay_valid(self):
        # §45 D-123/D-124: a publication takes the files as they are now; the cycle's
        # manifest is brought up to them first, so an edit is no longer "source mismatch".
        cycle = self._cycle([["a"], ["a"]])
        first = self._admit(cycle, 0)
        base = Path(cycle["cycle_dir"]) / "artifacts/gen1"
        digest_before = P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"]
        (base / "a/prd.md").write_text("edited after sealing")
        second = self._admit(cycle, 1, base_revision=first["shared_reference_revision_id"])
        self.assertEqual(second["status"], "admitted")
        self.assertEqual((Path(second["revision_dir"]) / "a/prd.md").read_text(), "edited after sealing")
        record = P.read_cycle_record(self.root, cycle["cycle_id"])
        self.assertNotEqual(record["manifest_digest"], digest_before)
        revision = json.loads((Path(second["revision_dir"]) / P.REVISION_RECORD_NAME).read_text())
        self.assertEqual(revision["source"]["manifest_digest"], record["manifest_digest"])
        # A malformed base receipt is still refused.
        receipt = base / P.SPEC_BASE_RECEIPT
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("tampered")
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(cycle, 1, base_revision=second["shared_reference_revision_id"])
        self.assertEqual(ctx.exception.code, "shared-base-invalid")

    def test_sealed_receipt_cannot_be_overridden_and_matches_actual_base(self):
        first_cycle = self._cycle([["a"]])
        first = self._admit(first_cycle, 0)
        route, route_file = self.route("direct", "autopilot-spec", "update", slug="receipt")
        cycle = P.begin(self.root, route_file=route_file, capability="autopilot-spec", intensity="direct")
        self.write_output(cycle, "gen0/a/prd.md", b"updated\n")
        receipt = {"schema_version": 1, "reference_id": first["shared_reference_id"],
                   "revision_id": first["shared_reference_revision_id"], "content_digest": first["content_digest"]}
        self.write_output(cycle, "gen0/" + P.SPEC_BASE_RECEIPT, json.dumps(receipt).encode())
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=cycle["cycle_id"])
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(cycle, 0, base_revision="none")
        self.assertEqual(ctx.exception.code, "shared-base-mismatch")
        self.assertEqual(self._admit(cycle, 0)["status"], "admitted")

    def test_malformed_sealed_receipt_cannot_fall_back_to_cli_base(self):
        self.activate()
        route, route_file, cycle = self.begin("direct", "autopilot-spec", "update")
        for index, raw in enumerate((b"not json", b"null", b"[]", b"{}")):
            self.write_output(cycle, f"gen{index}/a/prd.md", b"a\n")
            self.write_output(cycle, f"gen{index}/" + P.SPEC_BASE_RECEIPT, raw)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=cycle["cycle_id"])
        for index in range(4):
            with self.subTest(index=index), self.assertRaises(P.ProducerError) as ctx:
                self._admit(cycle, index, base_revision="none")
            self.assertEqual(ctx.exception.code, "shared-base-invalid")
        self.assertEqual(P.list_references(self.root, "spec"), [])
        self.assertEqual(self._journals(), [])

    def test_crash_recovery_and_old_journal_replay_never_rewind_latest(self):
        from unittest import mock
        cycle = self._cycle([["a"], ["a"], ["a"]])
        first = self._admit(cycle, 0)
        base = first["shared_reference_revision_id"]
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._admit(cycle, 1, base_revision=base)
        journal_path = P.shared_journal_path(self.root, "probe").parent
        journal_file = next(journal_path.glob("*.json"))
        journal_bytes = journal_file.read_bytes()
        journal = json.loads(journal_bytes)
        P._recover_locked(self.root)
        winner = self._admit(cycle, 2, base_revision=journal["revision_id"])
        journal_file.write_bytes(journal_bytes)
        P._recover_locked(self.root)
        self.assertFalse(journal_file.exists())
        self.assertEqual(self._reference(first["shared_reference_id"])["latest_revision_id"],
                         winner["shared_reference_revision_id"])
        # Model an uncommitted stale published journal. Keep it for inspection.
        reference = self._reference(first["shared_reference_id"])
        reference["revisions"].remove(journal["revision_id"])
        P._write_atomic(P._reference_path(self.root, "spec", first["shared_reference_id"]), P._json_bytes(reference))
        journal_file.write_bytes(journal_bytes)
        recovered = P._recover_locked(self.root)
        self.assertEqual(recovered["unresolved"][0]["code"], "shared-base-mismatch")
        self.assertTrue(journal_file.exists())
        self.assertEqual(self._reference(first["shared_reference_id"]), reference)


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
                key="prd", base_revision=admitted["shared_reference_revision_id"] if admitted else "none",
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
                       key="prd", base_revision=admitted["shared_reference_revision_id"], drop_components=["b"], drop_reason="retired")
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
        observed = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual({k: v for k, v in observed.items() if k not in {"control_observations", "control_baselines"}}, before)
        for operation in (P.verify_finalized_cycle, P.finalize_exact_cycle):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(P.ProducerError, "already-sealed-mismatch"):
                    operation(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(P.read_cycle_record(self.root, cycle_id), observed)
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
        # §45 D-127: the proof is record, manifest and index agreeing; edited files are the refresh's.
        output.write_bytes(b"drift after sealing\n")
        verified = P.verify_finalized_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)
        self.assertEqual(verified["status"], "already-sealed")
        self.assertEqual(manifest.read_bytes(), before)
        replay = P.finalize_exact_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)
        self.assertEqual(replay["status"], "already-sealed")
        self.assertEqual(manifest.read_bytes(), before)

    def test_completed_finalize_live_lease_and_reentry_make_no_manifest(self):
        result, output, binding = self.prepared()
        P.review_lease_acquire(self.root, cycle_id=result["cycle_id"], attempt_id="att-review")
        with self.assertRaisesRegex(P.ProducerError, "cycle-finalize-blocked-live-review"):
            P.finalize_exact_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)
        self.assertFalse((Path(result["cycle_dir"]) / "manifest.json").exists())
        with self.assertRaisesRegex(P.ProducerError, "finalize-reentry-forbidden"):
            P.finalize(self.root, cycle_id=result["cycle_id"], _admission_lock_fd=123)
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")


class InlineProducerBindingAdmissionLockTest(ProducerTestBase):
    def test_full_inline_binding_is_rechecked_after_admission_lock_acquisition(self):
        self.activate()
        route, route_file = self.route(campaign_key="lock-binding")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID":"", "AGENT_ROUTE_FILE":"",
                                          "AGENT_ROUTE_ID":"", "AGENT_ROUTE_NODE":""}):
            started = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                              intensity="direct", jobs=self.jobs)
        self.close(route, route_file)
        record = P.read_cycle_record(self.root, started["cycle_id"])
        campaign = P.read_campaign(self.root, record["campaign_id"])
        binding = {
            "kind": "inline_producer_binding_v1", "artifact_root_id": ROOT_ID,
            "campaign_key": campaign["key"], "campaign_id": record["campaign_id"],
            "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "cycle_record_digest": P.dispatch_terminal_commit.cycle_identity_digest(record),
            "terminal_marker_digest": "a" * 64, "evidence_sha256": "b" * 64,
            "inline_finish_id": "c" * 64,
        }
        slot = self.root / ".runtime/inline-finish/v1" / route["route_id"] / "finish.json"
        slot.parent.mkdir(parents=True)
        slot.write_text(json.dumps({
            "schema": "inline_finish_v1", "inline_finish_id": binding["inline_finish_id"],
            "terminal_marker_digest": binding["terminal_marker_digest"], "state": "route-closed",
            "intent": {key: binding[key] for key in (
                "route_id", "route_hash", "artifact_root_id", "campaign_key",
                "campaign_id", "cycle_id", "producer_id", "evidence_sha256")},
        }))
        # The first check must pass before the mocked lock changes the record.
        P._inline_producer_binding_check(self.root, record["cycle_id"], binding)
        acquire = adm._acquire_lock
        acquired = []

        def acquire_then_rebind(root, *args, **kwargs):
            fd = acquire(root, *args, **kwargs)
            acquired.append(fd)
            changed = dict(P.read_cycle_record(self.root, record["cycle_id"]))
            changed["route_hash"] = "sha256:" + "f" * 64
            P._write_cycle_record(self.root, changed, exclusive=False)
            return fd

        with mock.patch.object(adm, "_acquire_lock", side_effect=acquire_then_rebind):
            with self.assertRaisesRegex(P.ProducerError, "inline-producer-binding-mismatch"):
                P.finalize_exact_cycle(self.root, cycle_id=record["cycle_id"], expected_binding=binding)
        self.assertEqual(len(acquired), 1, "finalize must reach and recheck under the admission lock")
        self.assertFalse((Path(started["cycle_dir"]) / "manifest.json").exists())
        self.assertEqual(P.read_cycle_record(self.root, record["cycle_id"])["state"], "open")


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
        import worker_bootstrap
        scope = worker_bootstrap.resolve_node_scope(route, node["id"], {}, parent_attempt_id=owner)
        self.assertEqual(scope.source, "producer-binding")

    def test_all_launch_wrappers_bind_after_claim_before_spawn(self):
        root = Path(__file__).resolve().parents[1]
        for harness in ("claude", "codex", "opencode"):
            source = (root / "adapters" / harness / "bin" / "dispatch-headless.py").read_text()
            self.assertIn("bind_owner_launch,", source)
            call = source.index("bind_owner_launch(args, jobs)")
            self.assertLess(source.index("prompt_path.write_text(prompt_text"), call)
            self.assertLess(call, source.index("spawn_claimed_attempt(", call))


class TerminalTransactionIntegrationTest(ProducerTestBase):
    def test_closed_unstarted_continuation_does_not_take_spec_settlement(self):
        import dispatch_terminal_commit as terminal
        from dispatch_completion_join import exact_attempt_row

        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture = TerminalTransactionIntegrationTest()
                fixture.setUp()
                try:
                    route, path, jobs, owner, cycle, review, request = fixture._prepare_fixture(harness, "autopilot-spec")
                    successor = R.build_continuation_route(
                        route, resume_from_node="review", requested_boundary="review",
                        reason="batch-refused-before-start", artifact_root=fixture.root)
                    successor_file = R.canonical_route_path(fixture.root, successor["route_id"])
                    R.publish_continuation_route(successor, route, successor_file)
                    batch_spec = importlib.util.spec_from_file_location("unstarted_batch", Path(__file__).with_name("dispatch-batch.py"))
                    batch = importlib.util.module_from_spec(batch_spec)
                    batch_spec.loader.exec_module(batch)
                    batch_output = io.StringIO()
                    before_batch = jobs.read_bytes()
                    with mock.patch.object(batch, "load_route", return_value=successor), \
                            mock.patch.object(batch.subprocess, "Popen") as spawn, \
                            contextlib.redirect_stdout(batch_output), contextlib.redirect_stderr(io.StringIO()):
                        code = batch.main(["--action", "start", "--route", str(successor_file),
                                           "--parallel-group", "research", "--parent", "owner",
                                           "--slug-prefix", "refused", "--jobs", str(jobs)])
                    self.assertEqual(code, 65, batch_output.getvalue())
                    self.assertEqual(json.loads(batch_output.getvalue())["reason"], "parallel-group-cardinality")
                    spawn.assert_not_called()
                    self.assertEqual(jobs.read_bytes(), before_batch)
                    # Old publish-time audit bindings must recover without editing records.
                    R.bind_continuation_cycle(fixture.root, route, successor)
                    outcome, _ = R.close_route(successor, successor_file, summary="batch admitted 0, spawned 0")
                    self.assertFalse(outcome["terminal_gate_proven"])
                    report = fixture.write_output(cycle, rel="spec/_internal/owner-report.md",
                                                  data=b"verified transaction report\n")
                    text = f"artifact: {report}\nverdict: PASS\nblocker: none"
                    native = {
                        "codex": [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                                  {"type": "turn.completed"}],
                        "claude": [{"type": "result", "subtype": "success", "is_error": False, "result": text}],
                        "opencode": [{"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": text}},
                                     {"type": "step_finish", "sessionID": "ses_test", "part": {"type": "step-finish", "reason": "stop"}}],
                    }[harness]
                    log = jobs.parent / "owner.jsonl"
                    log.write_text("\n".join(json.dumps(row) for row in native) + "\n")
                    jobs.write_text(jobs.read_text().replace("worker_type=owner", f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
                    fixture._closed_owner(jobs, owner)
                    metadata = exact_attempt_row(jobs, owner).metadata
                    before_jobs = jobs.read_bytes()
                    before_successor = successor_file.read_bytes()
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                        settled = terminal.settle_owner_completion(jobs, "done", metadata)
                        self.assertEqual(settled.result, "completed", settled)
                        self.assertEqual(P.read_cycle_record(fixture.root, cycle["cycle_id"])["state"], "sealed")
                        manifest = json.loads((Path(cycle["cycle_dir"]) / "manifest.json").read_text())
                        self.assertEqual(manifest["routes"][0]["route_id"], route["route_id"])
                        self.assertEqual(settled.shared_publication["status"], "admitted", settled.shared_publication)
                        references = P.list_references(fixture.root, "spec")
                        self.assertEqual(len(references), 1)
                        self.assertEqual(terminal.settle_owner_completion(jobs, "done", metadata).result, "completed")
                        self.assertEqual(jobs.read_bytes(), before_jobs)
                        self.assertEqual(successor_file.read_bytes(), before_successor)
                finally:
                    fixture.doCleanups()

    def _prepare_fixture(self, harness="claude", capability="autopilot-code"):
        import dispatch_terminal_commit as terminal
        self.activate()
        route=R.compile_route(capability,"update" if capability=="autopilot-spec" else "dev","standard",cwd=R.ROOT,artifact_root=self.root,
            predicates=[],transport="headless",tracking="tracked",tracked_gate_evidence=gate_evidence(),
            slug="terminal-transaction-fixture",dispatch_evidence={"tuples":[nested(harness,"codex")]})
        if capability=="autopilot-spec":
            route=R.compose_route(capability=capability,capability_mode="update",shape="staged",
                graph="review,prd-transaction",slug="terminal-transaction-fixture",cwd=R.ROOT,
                artifact_root=self.root,intensity="standard",dispatch_evidence={"tuples":[nested(harness,"codex")]},
                unassigned=True)
        route_file=Path(L.admit_runtime_route(self.root,route).route_file)
        jobs=Path(self._tmp.name)/"jobs.log"; owner="att-transaction-owner"; child="att-transaction-report"
        owner_meta=dict(attempt_id=owner,worker_type="owner",unit="_kernel/owner",dispatch_depth="1",
            registered_worker="1",harness=harness,owner_route_file=str(route_file),
            owner_route_id=route["route_id"],owner_route_hash=route["route_hash"])
        def row(status,slug,metadata):
            return f"2026-09-08T00:00:00Z\t{status}\t{R.ROOT}\t{R.ROOT}\t{slug}\t"+",".join(f"{k}={v}" for k,v in metadata.items())+"\n"
        jobs.write_text(row("open","owner",owner_meta))
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
            result=P.begin(self.root,route_file=route_file,capability=capability,intensity="standard",
                           jobs=jobs,owner_attempt_id=owner)
            if capability == "autopilot-spec":
                self.write_output(result, rel="spec/prd.md", data=b"# Official transaction PRD\n")
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
                        # The settled owner row preserves the completion even though an
                        # owner-executed terminal has no worker marker file.
                        self.assertFalse(terminal.owner_completion_pending(jobs,"done",meta))
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

    def test_official_continuation_owner_settlement_recovers_published_manifest_before_seal(self):
        import dispatch_terminal_commit as terminal

        source, source_file, jobs, _source_owner, begun, artifact, _source_request = self._prepare_fixture()
        begin_record = P.read_cycle_record(self.root, begun["cycle_id"])
        begin_identity = {key: begin_record[key] for key in ("route_id", "route_hash", "route_file")}
        expected_input_digest = P._digest(P._canonical({
            "route_id": begin_record["route_id"], "route_hash": begin_record["route_hash"],
            "capability": begin_record["capability"], "intensity": begin_record["intensity"],
        }))
        terminal_node = next(node for node in source["nodes"] if node.get("terminal") is True)
        first_node = source["nodes"][0]
        continuation = R.build_continuation_route(
            source, resume_from_node=first_node["id"], requested_boundary=first_node["id"],
            reason="terminal-settlement-regression", artifact_root=self.root,
        )
        continuation_file = R.canonical_route_path(self.root, continuation["route_id"])
        R.publish_continuation_route(continuation, source, continuation_file)
        self.assertTrue(R.bind_continuation_cycle(self.root, source, continuation)["bound"])

        owner = "att-continuation-owner"
        child = "att-continuation-report"
        def row(status, slug, metadata):
            return (f"2026-09-08T00:00:00Z\t{status}\t{R.ROOT}\t{R.ROOT}\t{slug}\t" +
                    ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n")
        owner_meta = dict(attempt_id=owner, worker_type="owner", unit="_kernel/owner", dispatch_depth="1",
            registered_worker="1", harness="claude", owner_route_file=str(continuation_file),
            owner_route_id=continuation["route_id"], owner_route_hash=continuation["route_hash"])
        child_meta = dict(attempt_schema_version="2", attempt_id=child, parent_attempt_id=owner,
            dispatch_depth="2", transport="headless", execution_surface="registered-headless",
            registered_worker="1", fallback_hop="same-harness-headless", harness="codex",
            route_id=continuation["route_id"], route_hash=continuation["route_hash"],
            route_node=terminal_node["id"], failure_class="pass", launch_outcome="reaped-before-publish")
        jobs.write_text(jobs.read_text() + row("open", "continuation-owner", owner_meta)
                        + row("open", "continuation-report", child_meta))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(self.root)}):
            resumed = P.begin(self.root, route_file=continuation_file, capability="autopilot-code",
                               intensity="standard", jobs=jobs, owner_attempt_id=owner)
            self.assertEqual((resumed["status"], resumed.get("rebound"), resumed["cycle_id"]),
                             ("resumed", True, begun["cycle_id"]))
            self.assertEqual(len(P.list_cycle_records(self.root)), 1)
            self.assertTrue(terminal.load_producer_binding(
                artifact_root=self.root, route_id=continuation["route_id"], owner_attempt_id=owner).binding)
            self.assertEqual({key: P.read_cycle_record(self.root, begun["cycle_id"])[key]
                              for key in begin_identity}, begin_identity)

            continuation_node = next(node for node in continuation["nodes"] if node.get("terminal") is True)
            R.complete_node(continuation, continuation_node, continuation_node["id"], artifact,
                            jobs=jobs, attempt_id=child)
            self._closed_owner(jobs, owner)
            request = terminal.TerminalCommitRequest(continuation_file, owner, jobs, self.root)
            proof = terminal.prove_terminal_authority(request)
            self.assertEqual(proof.status, "proved", proof)
            commit_path = terminal._commit_state_path(request)

            with mock.patch.object(P, "_commit_sealed", side_effect=RuntimeError("crash-before-seal")):
                interrupted = terminal.settle_terminal_commit(request)
            self.assertNotEqual(interrupted.result, "completed", interrupted)
            manifest = Path(begun["cycle_dir"]) / "manifest.json"
            published_bytes = manifest.read_bytes()
            published = json.loads(published_bytes)
            self.assertEqual(len(published["routes"]), 1)
            self.assertEqual(published["routes"][0]["route_id"], continuation["route_id"])
            self.assertEqual(published["cycle"]["input_digest"], expected_input_digest)
            open_record = P.read_cycle_record(self.root, begun["cycle_id"])
            self.assertEqual(open_record["state"], "open")
            self.assertEqual((open_record["route_id"], open_record["route_hash"]),
                             (begin_identity["route_id"], begin_identity["route_hash"]))
            self.assertEqual(open_record["route_file"], begin_identity["route_file"])

            recovered = terminal.settle_terminal_commit(request)
            self.assertEqual(recovered.result, "completed", recovered)
            sealed_bytes = manifest.read_bytes()
            commit_id = json.loads(commit_path.read_text())["terminal_commit_id"]
            self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
            replay = terminal.settle_terminal_commit(request)
            self.assertEqual(replay.result, "completed", replay)
            self.assertEqual(manifest.read_bytes(), sealed_bytes)
            self.assertEqual(json.loads(commit_path.read_text())["terminal_commit_id"], commit_id)
            self.assertEqual(json.loads(manifest.read_text())["routes"][0]["route_id"], continuation["route_id"])

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
                    # §45 D-127: a settled completion stays complete when its report is edited afterwards.
                    artifact.write_text("changed after sealing")
                    self.assertFalse(terminal.owner_completion_pending(jobs,"done",meta))
                    self.assertEqual(terminal.completed_owner_handoff(jobs,"done",meta).count(str(artifact)),1)
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

    def _claimed_owner_with_false_close(self, **different):
        """The owner's real claim (terminal-commit slot, producer binding, current marker) and, before it
        settles, an earlier caller's close recorded unproven with the tuple that claim holds."""
        import dispatch_terminal_commit as terminal
        route,route_file,jobs,owner,result,artifact,request=self._prepare_fixture()
        self._closed_owner(jobs,owner)
        crashed=terminal.settle_terminal_commit(
            request,terminal.TerminalCommitServices(crash_after="claim-after"))
        self.assertNotEqual(crashed.result,"completed",crashed)
        state=json.loads(terminal._commit_state_path(request).read_text())
        self.assertEqual(state["state"],"claimed")
        held=dict(terminal_commit_id=state["terminal_commit_id"],expected_owner_attempt_id=owner,
                  expected_producer_binding_digest=state["producer_binding_digest"],
                  expected_terminal_marker_digest=state["terminal_marker_digest"])
        held.update(different)
        unproven={"report":{"passed":False,"reason":"completion-attempt-not-current"}}
        with mock.patch.object(R,"terminal_gate_observation",return_value=unproven):
            first,created=R.close_route(route,route_file,allow_unproven=True,jobs=jobs,**held)
        self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
        return route,route_file,jobs,owner,result,request,R.outcome_path(route_file).read_bytes(),state

    def test_identity_bearing_false_close_of_the_actual_claim_is_consumed_by_a_normal_close_and_sealed_once(self):
        import dispatch_terminal_commit as terminal
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state=self._claimed_owner_with_false_close()
            current,created=R.close_route(route,route_file)  # no identity, no registry: the runtime reads its own state
            self.assertTrue(created); self.assertTrue(current["terminal_gate_proven"])
            self.assertEqual((current["terminal_commit_id"],current["terminal_owner_attempt_id"],
                              current["producer_binding_digest"],current["terminal_marker_digest"]),
                             (state["terminal_commit_id"],owner,state["producer_binding_digest"],
                              state["terminal_marker_digest"]))
            retained=list(Path(route_file).parent.glob("*.historical-false-*.outcome.json"))
            self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)
            settled_outcome=R.outcome_path(route_file).read_bytes()
            settled=terminal.settle_terminal_commit(request)  # the owner's own settlement carries on from its claim
            self.assertEqual(settled.result,"completed",settled)
            manifest=Path(result["cycle_dir"])/"manifest.json"
            self.assertEqual(json.loads(manifest.read_text())["cycle"]["state"],"completed")
            self.assertEqual(R.outcome_path(route_file).read_bytes(),settled_outcome)
            self.assertEqual(len(list(Path(route_file).parent.glob("*.historical-false-*.outcome.json"))),1)
            sealed=manifest.read_bytes()
            self.assertEqual(terminal.settle_terminal_commit(request).result,"completed")
            self.assertEqual(manifest.read_bytes(),sealed)
            events=json.loads(sealed)["events"]
            self.assertEqual(len([e for e in events if e["event_type"]=="cycle.completed"]),1)
            self.assertEqual(len([e for e in events if e["event_type"]=="route.terminal.recorded"]),1)

    def test_owner_settlement_consumes_its_own_identity_bearing_false_close_once(self):
        import dispatch_terminal_commit as terminal
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state=self._claimed_owner_with_false_close()
            settled=terminal.settle_terminal_commit(request)
            self.assertEqual(settled.result,"completed",settled)
            outcome=json.loads(R.outcome_path(route_file).read_text())
            self.assertTrue(outcome["terminal_gate_proven"])
            self.assertEqual(outcome["terminal_commit_id"],state["terminal_commit_id"])
            retained=list(Path(route_file).parent.glob("*.historical-false-*.outcome.json"))
            self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)
            self.assertEqual(P.read_cycle_record(self.root,result["cycle_id"])["state"],"sealed")

    def test_identity_bearing_false_close_of_another_owner_is_not_consumed_or_sealed(self):
        import dispatch_terminal_commit as terminal
        for label,different in (("another owner",dict(expected_owner_attempt_id="att-other-owner")),
                                ("another binding",dict(expected_producer_binding_digest="sha256:"+"9"*64)),
                                ("another marker",dict(expected_terminal_marker_digest="sha256:"+"8"*64)),
                                ("another commit",dict(terminal_commit_id="7"*64))):
            with self.subTest(label):
                fixture=TerminalTransactionIntegrationTest(); fixture.setUp()
                try:
                    with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(fixture._tmp.name)/"jobs.log")}):
                        route,route_file,jobs,owner,result,request,raw,state=fixture._claimed_owner_with_false_close(**different)
                        with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
                            R.close_route(route,route_file)  # nothing named: still not this owner's marker
                        settled=terminal.settle_terminal_commit(request)
                        self.assertNotEqual(settled.result,"completed",settled)
                        self.assertEqual(R.outcome_path(route_file).read_bytes(),raw)
                        self.assertEqual(list(Path(route_file).parent.glob("*.historical-false-*")),[])
                        self.assertNotEqual(P.read_cycle_record(fixture.root,result["cycle_id"])["state"],"sealed")
                finally:
                    fixture.doCleanups()

    def _foreign_binding_false(self, *, drop_binding):
        """The actual claim (owner row, producer binding, child marker, claimed slot) plus an earlier unproven
        close that recorded the current owner and marker but ANOTHER producer binding digest and the commit id
        computed from it. The slot keeps the legitimate digest and id; `drop_binding` removes the binding file
        of this temporary fixture root, so only the slot still knows the current binding."""
        import dispatch_terminal_commit as terminal
        route,route_file,jobs,owner,result,artifact,request=self._prepare_fixture()
        self._closed_owner(jobs,owner)
        crashed=terminal.settle_terminal_commit(request,terminal.TerminalCommitServices(crash_after="claim-after"))
        self.assertNotEqual(crashed.result,"completed",crashed)
        state=json.loads(terminal._commit_state_path(request).read_text())
        self.assertEqual(state["state"],"claimed")
        foreign="sha256:"+"5"*64
        self.assertNotEqual(foreign,state["producer_binding_digest"])
        foreign_id=terminal.terminal_commit_id(route_id=route["route_id"],route_hash=route["route_hash"],
            owner_attempt_id=owner,marker_digest=state["terminal_marker_digest"],producer_digest=foreign)
        self.assertNotEqual(foreign_id,state["terminal_commit_id"])
        held=dict(terminal_commit_id=foreign_id,expected_owner_attempt_id=owner,
                  expected_producer_binding_digest=foreign,
                  expected_terminal_marker_digest=state["terminal_marker_digest"])
        unproven={"report":{"passed":False,"reason":"completion-attempt-not-current"}}
        with mock.patch.object(R,"terminal_gate_observation",return_value=unproven):
            first,created=R.close_route(route,route_file,allow_unproven=True,jobs=jobs,**held)
        self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
        raw=R.outcome_path(route_file).read_bytes()
        binding_path=terminal.producer_binding_path(self.root,route["route_id"],owner)
        self.assertTrue(binding_path.is_file())
        if drop_binding:
            binding_path.unlink()
        return route,route_file,jobs,owner,result,request,raw,state,held,binding_path

    def _foreign_binding_snapshot(self, route_file, request, result, binding_path):
        import dispatch_terminal_commit as terminal
        return dict(outcome=R.outcome_path(route_file).read_bytes(),
                    slot=terminal._commit_state_path(request).read_bytes(),
                    binding=binding_path.read_bytes() if binding_path.is_file() else None,
                    cycle_state=P.read_cycle_record(self.root,result["cycle_id"])["state"],
                    retained=sorted(p.name for p in Path(route_file).parent.glob("*.historical-false-*")))

    def test_historical_foreign_binding_false_is_not_consumed_while_the_current_binding_exists(self):
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state,held,binding_path=self._foreign_binding_false(drop_binding=False)
            before=self._foreign_binding_snapshot(route_file,request,result,binding_path)
            for label,kwargs in (("nothing named",{}),("historical tuple re-supplied",held)):
                with self.subTest(label):
                    with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
                        R.close_route(route,route_file,**kwargs)
                    self.assertEqual(before,self._foreign_binding_snapshot(route_file,request,result,binding_path))

    def test_historical_foreign_tuple_is_not_proven_by_resupply_when_the_binding_file_is_absent(self):
        """The binding file is gone but the claimed slot still holds the current digest/id: a caller that
        re-supplies the historical (other digest, other id) cannot make it the current proof."""
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state,held,binding_path=self._foreign_binding_false(drop_binding=True)
            before=self._foreign_binding_snapshot(route_file,request,result,binding_path)
            for label,kwargs in (("historical tuple re-supplied",held),("nothing named",{})):
                with self.subTest(label):
                    with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
                        R.close_route(route,route_file,**kwargs)
                    self.assertEqual(before,self._foreign_binding_snapshot(route_file,request,result,binding_path))
            self.assertEqual(before["retained"],[])
            self.assertNotEqual(before["cycle_state"],"sealed")

    def test_complete_hook_does_not_consume_a_foreign_binding_false_when_the_binding_file_is_absent(self):
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state,held,binding_path=self._foreign_binding_false(drop_binding=True)
            before=self._foreign_binding_snapshot(route_file,request,result,binding_path)
            with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
                R._promote_historical_false_outcome(route,route_file,json.loads(raw),raw,jobs=jobs)
            self.assertEqual(before,self._foreign_binding_snapshot(route_file,request,result,binding_path))

    def test_actual_slot_tuple_is_consumed_even_when_the_binding_file_is_absent(self):
        """The current tuple is derived from the verified claimed slot, so the real tuple still promotes once."""
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state=self._claimed_owner_with_false_close()
            import dispatch_terminal_commit as terminal
            terminal.producer_binding_path(self.root,route["route_id"],owner).unlink()
            out,created=R.close_route(route,route_file)
            self.assertTrue(created); self.assertTrue(out["terminal_gate_proven"])
            self.assertEqual((out["terminal_commit_id"],out["producer_binding_digest"]),
                             (state["terminal_commit_id"],state["producer_binding_digest"]))
            retained=list(Path(route_file).parent.glob("*.historical-false-*.outcome.json"))
            self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)

    def test_unsettled_false_close_is_returned_unchanged_and_does_not_seal_the_cycle(self):
        import dispatch_terminal_commit as terminal
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(self._tmp.name)/"jobs.log")}):
            route,route_file,jobs,owner,result,request,raw,state=self._claimed_owner_with_false_close()
            unproven={"report":{"passed":False,"reason":"completion-attempt-not-current"}}
            manifest=Path(result["cycle_dir"])/"manifest.json"
            manifest_before=manifest.read_bytes() if manifest.exists() else None
            with mock.patch.object(R,"terminal_gate_observation",return_value=unproven):
                out,created=R.close_route(route,route_file)
            self.assertFalse(created); self.assertFalse(out["terminal_gate_proven"])
            self.assertNotEqual(P.read_cycle_record(self.root,result["cycle_id"])["state"],"sealed")
            self.assertEqual(manifest.read_bytes() if manifest.exists() else None,manifest_before)
            self.assertEqual(R.outcome_path(route_file).read_bytes(),raw)
            self.assertEqual(terminal.settle_terminal_commit(request).result,"completed")

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
            # §45 D-127: the stored envelope is replayed as it was; a report edited or removed
            # after the settlement is news beside it, not a failure.
            stored=slot/"owner-envelope.txt"
            artifact.write_text("post-seal edit")
            edited=terminal.settle_terminal_commit(request)
            self.assertEqual((edited.result,edited.detail),("completed","primary-changed-after-seal"),edited)
            self.assertEqual(edited.envelope_text,stored.read_text())
            artifact.unlink()
            removed=terminal.settle_terminal_commit(request)
            self.assertEqual((removed.result,removed.detail),("completed","primary-missing-after-seal"),removed)
            self.assertEqual(before,{str(path):path.read_bytes() for path in paths})

    def test_payload_settlement_recovers_and_verifies_bytes_for_three_harnesses(self):
        import dispatch_terminal_commit as terminal
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture = TerminalTransactionIntegrationTest(); fixture.setUp()
                try:
                    route, path, jobs, owner, result, report, request = fixture._prepare_fixture(harness)
                    rel = "evidence/visual/test-results/last-run.json"
                    payload = b'{"status":"passed","failedTests":[]}\n'
                    hidden = fixture.write_output(result, rel=rel, data=payload)
                    # A hidden sibling (Playwright's own dot file) is outside the inclusion rule (§45 D-123).
                    fixture.write_output(result, rel="evidence/visual/test-results/.last-run.json", data=payload)
                    fixture._closed_owner(jobs, owner)
                    registry = jobs.read_bytes()
                    marker = R.completion_dir(route["route_id"], jobs=jobs) / "report.json"
                    marker_bytes = marker.read_bytes()
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                        # Crash after manifest publication; exact finish owns recovery.
                        with mock.patch.object(P, "_commit_sealed", side_effect=RuntimeError("crash")):
                            interrupted = terminal.settle_terminal_commit(request)
                        self.assertNotEqual(interrupted.result, "completed", interrupted)
                        manifest = Path(result["cycle_dir"]) / "manifest.json"
                        sealed = manifest.read_bytes()
                        doc = json.loads(sealed)
                        hidden = Path(result["cycle_dir"]) / "artifacts" / rel
                        self.assertNotIn("artifacts/evidence/visual/test-results/.last-run.json",
                                         [r["locator"]["path"] for r in doc["artifact_revisions"]])
                        revision = next(r for r in doc["artifact_revisions"]
                                        if r["locator"]["path"] == "artifacts/" + rel)
                        self.assertEqual(revision["content_digest"], "sha256:" + hashlib.sha256(payload).hexdigest())
                        self.assertEqual(revision["byte_size"], len(payload))
                        for _ in range(2):
                            settled = terminal.settle_terminal_commit(request)
                            self.assertEqual(settled.result, "completed", settled)
                            self.assertEqual(manifest.read_bytes(), sealed)
                            self.assertEqual(hidden.read_bytes(), payload)
                        # §45 D-127: a payload edited after the settlement is not a failed settlement.
                        hidden.write_bytes(payload + b"drift")
                        self.assertEqual(terminal.settle_terminal_commit(request).result, "completed")
                        self.assertEqual(manifest.read_bytes(), sealed)
                        self.assertEqual(marker.read_bytes(), marker_bytes)
                        self.assertEqual(jobs.read_bytes(), registry)
                finally:
                    fixture.doCleanups()

    def test_payload_names_with_research_punctuation_settle_with_their_bytes(self):
        # A lab cycle stayed at route-closed with producer-finalize-failed because
        # session ranges (`0000~0004`), combined configs (`CDR+MCWF`), copies
        # (`input(004)`), spaced paper titles and 134-character names were refused.
        import dispatch_terminal_commit as terminal
        route, route_file, jobs, owner, result, artifact, request = self._prepare_fixture()
        names = {"raw/672-122797-0000~0004_doa.npz": b"npz\n",
                 "replay/REL+UP3/rows.csv": b"a,b\n",
                 "examples/input(004).wav": b"RIFF\n",
                 "ref/A unified convolutional beamformer.pdf": b"%PDF-1.7\n",
                 "policy/AVG-L8@tL/lr=1e-3,bs=32.json": b"{}\n",
                 "ref/" + "b" * 130 + ".pdf": b"%PDF-1.4\n"}
        for rel, data in names.items():
            self.write_output(result, rel=rel, data=data)
        self._closed_owner(jobs, owner)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
            settled = terminal.settle_terminal_commit(request)
        self.assertEqual(settled.result, "completed", settled)
        doc = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_bytes())
        digests = {r["locator"]["path"]: r["content_digest"] for r in doc["artifact_revisions"]}
        for rel, data in names.items():
            with self.subTest(rel=rel):
                self.assertEqual(digests.get("artifacts/" + rel),
                                 "sha256:" + hashlib.sha256(data).hexdigest())

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
        # The campaign carries the stream key, not the route slug.
        self.assertTrue(locator.endswith("_streaming-release"), locator)
        self.assertEqual(campaign["slug"], "streaming-release")
        self.assertEqual(campaign["slug_source"], "campaign-key")
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
    def test_direct_start_returns_one_real_cycle_even_with_foreign_inherited_env(self):
        import work_start
        self.activate()
        from route_identity import route_hash, route_id_from_hash
        route = compile_for("direct", self.root)
        route["work_request"] = {"text": "Apply the approved small fix", "owner_harness": None}
        route["route_hash"] = route_hash(route)
        route["route_id"] = route_id_from_hash(route["route_hash"])
        path = Path(L.admit_runtime_route(self.root, route).route_file)
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CYCLE_ID": "cyc_foreign",
                                        "AGENT_ARTIFACT_OUTPUT_DIR": "/foreign/artifacts"}):
            first = work_start.start_work(route, path, self.jobs)
            second = work_start.start_work(route, path, self.jobs)
        self.assertEqual(first["state"], "inline", first)
        self.assertEqual(second["artifact_env"], first["artifact_env"])
        self.assertEqual(len(list(P.list_cycle_records(self.root))), 1)
        self.assertTrue(Path(first["artifact_env"]["AGENT_ARTIFACT_OUTPUT_DIR"]).is_dir())

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


_ADMISSION_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
stat = os.fstat(fd)
print("held", stat.st_dev, stat.st_ino, flush=True)
time.sleep(float(sys.argv[2]))
"""


class OwnerLaunchBindingTest(ProducerTestBase):
    def _prepared_owner(self):
        from types import SimpleNamespace
        self.activate()
        route, route_file = self.route("quick")
        launch_env = P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)
        owner = "att-launch-binding"
        node = route["nodes"][0]
        metadata = dict(attempt_id=owner, worker_type="owner", dispatch_depth="1",
            registered_worker="1", harness="codex", capability="autopilot-code",
            capability_mode="dev", intensity="quick", route_file=str(route_file),
            route_id=route["route_id"], route_hash=route["route_hash"], route_node=node["id"],
            registry_digest=route["registry_digest"], completion_gate=node["completion_gate"],
            write_scope=";".join(node["write_scope"]))
        self.jobs.write_text("2026-09-27T00:00:00Z\topen\t" + str(R.ROOT) + "\t" + str(R.ROOT)
            + "\towner\t" + ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n", encoding="utf-8")
        args = SimpleNamespace(worker_type="owner", dispatch_depth=1, route_file=str(route_file),
                               owner_route_binding=None, attempt_id=owner)
        return route, route_file, launch_env, owner, args

    def _hold_admission_lock(self, root, seconds):
        """Hold the root's real admission flock from another process; return once it holds."""
        lock_path = P.artifact_admission._lock_file_path(root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", _ADMISSION_HOLDER, str(lock_path), str(seconds)],
                                  stdout=subprocess.PIPE, text=True)

        def reap():
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.addCleanup(reap)
        held = holder.stdout.readline().split()
        self.assertEqual(held[0], "held")
        stat = os.stat(P.artifact_admission._lock_file_path(root))
        self.assertEqual((int(held[1]), int(held[2])), (stat.st_dev, stat.st_ino))
        probe = os.open(str(lock_path), os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        return holder

    def test_owner_launch_waits_out_a_briefly_held_admission_lock(self):
        route, route_file, launch_env, owner, args = self._prepared_owner()
        root = Path(route["artifact_root"]).resolve()
        self._hold_admission_lock(root, 1.5)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}), \
                mock.patch.object(P.artifact_admission, "LOCK_TIMEOUT_DEFAULT", 0.5), \
                mock.patch.object(P, "OWNER_LAUNCH_ADMISSION_WAIT_SECONDS", 5.0, create=True):
            result = P.bind_owner_launch(args, self.jobs, environ=launch_env)
        self.assertEqual(result["cycle_id"], launch_env["AGENT_ARTIFACT_CYCLE_ID"])

    def test_owner_launch_at_the_wait_bound_reports_admission_busy(self):
        route, route_file, launch_env, owner, args = self._prepared_owner()
        root = Path(route["artifact_root"]).resolve()
        binding = P.dispatch_terminal_commit.producer_binding_path(self.root, route["route_id"], owner)
        holder = self._hold_admission_lock(root, 30)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}), \
                mock.patch.object(P.artifact_admission, "LOCK_TIMEOUT_DEFAULT", 0.5), \
                mock.patch.object(P, "OWNER_LAUNCH_ADMISSION_WAIT_SECONDS", 1.0, create=True):
            with self.assertRaises(P.ProducerError) as caught:
                P.bind_owner_launch(args, self.jobs, environ=launch_env)
            self.assertEqual(caught.exception.code, "admission-busy")
            self.assertIn("run start again later", caught.exception.detail)
            self.assertFalse(binding.exists())
            holder.kill()
            holder.wait()
            result = P.bind_owner_launch(args, self.jobs, environ=launch_env)
        self.assertEqual(result["cycle_id"], launch_env["AGENT_ARTIFACT_CYCLE_ID"])

    def _unbegun_route(self):
        self.activate()
        route, route_file = self.route()
        root = Path(route["artifact_root"]).resolve()
        self.assertEqual(list(P.list_cycle_records(root)), [])
        return route, route_file, root

    def test_owner_preparation_waits_out_a_briefly_held_admission_lock(self):
        route, route_file, root = self._unbegun_route()
        self._hold_admission_lock(root, 1.5)
        with mock.patch.object(P.artifact_admission, "LOCK_TIMEOUT_DEFAULT", 0.5), \
                mock.patch.object(P, "OWNER_LAUNCH_ADMISSION_WAIT_SECONDS", 5.0):
            env = P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)
        self.assertTrue(env["AGENT_ARTIFACT_CYCLE_ID"])
        self.assertEqual(len(list(P.list_cycle_records(root))), 1)

    def test_owner_preparation_at_the_wait_bound_reports_admission_busy(self):
        route, route_file, root = self._unbegun_route()
        holder = self._hold_admission_lock(root, 30)
        with mock.patch.object(P.artifact_admission, "LOCK_TIMEOUT_DEFAULT", 0.5), \
                mock.patch.object(P, "OWNER_LAUNCH_ADMISSION_WAIT_SECONDS", 1.0):
            with self.assertRaises(P.ProducerError) as caught:
                P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)
            self.assertEqual(caught.exception.code, "admission-busy")
            self.assertIn("run start again later", caught.exception.detail)
            self.assertEqual(list(P.list_cycle_records(root)), [])   # nothing was written
            holder.kill()
            holder.wait()
            env = P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)
        self.assertTrue(env["AGENT_ARTIFACT_CYCLE_ID"])

    def test_resume_only_owner_launch_publishes_and_replays_binding(self):
        route, route_file, launch_env, owner, args = self._prepared_owner()
        self.assertFalse(P.dispatch_terminal_commit.producer_binding_path(
            self.root, route["route_id"], "att-launch-binding").exists())
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}):
            result = P.bind_owner_launch(args, self.jobs, environ=launch_env)
            binding_path = P.dispatch_terminal_commit.producer_binding_path(self.root, route["route_id"], owner)
            first = binding_path.read_bytes()
            replay = P.bind_owner_launch(args, self.jobs, environ=launch_env)
            self.assertEqual(replay["status"], "resumed")
            self.assertEqual(binding_path.read_bytes(), first)
            self.assertEqual(result["cycle_id"], launch_env["AGENT_ARTIFACT_CYCLE_ID"])
            import worker_bootstrap
            scope = worker_bootstrap.resolve_node_scope(
                route, route["nodes"][0]["id"], {}, parent_attempt_id=owner)
            self.assertEqual(scope.source, "producer-binding")
            owner_env = {"AGENT_DISPATCH_ATTEMPT_ID": owner, "AGENT_DISPATCH_JOBS": str(self.jobs)}
            with mock.patch.dict(os.environ, owner_env):
                P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="quick",
                        jobs=self.jobs, owner_attempt_id=owner)
            self.assertEqual(binding_path.read_bytes(), first)
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)

    def test_replacement_owner_binds_from_the_routes_own_open_cycle(self):
        route, route_file, launch_env, original, args = self._prepared_owner()
        replacement = "att-launch-binding-replacement"
        line = self.jobs.read_text(encoding="utf-8").rstrip("\n").split("\t")
        meta = D.parse_registry_metadata(line[5])
        meta.update(attempt_id=replacement, automatic_retry_of=original)
        line[1], line[5] = "open", ",".join(f"{k}={v}" for k, v in meta.items())
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write("\t".join(line) + "\n")
        args.attempt_id = replacement
        caller = {"AGENT_ARTIFACT_CYCLE_ID": "cyc_stale", "AGENT_ARTIFACT_OUTPUT_DIR": "/stale/artifacts"}
        binding = P.dispatch_terminal_commit.producer_binding_path(self.root, route["route_id"], replacement)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}):
            with self.assertRaises(P.ProducerError) as caught:
                P.bind_owner_launch(args, self.jobs, environ=caller)
            self.assertEqual(caught.exception.code, "producer-binding-mismatch")
            self.assertFalse(binding.exists())
            route_env = P.prepare_route_artifact_env(route_file, start=False, jobs=self.jobs)
            self.assertEqual(route_env["AGENT_ARTIFACT_CYCLE_ID"], launch_env["AGENT_ARTIFACT_CYCLE_ID"])
            result = P.bind_owner_launch(args, self.jobs, environ={**caller, **route_env})
        self.assertEqual(result["cycle_id"], launch_env["AGENT_ARTIFACT_CYCLE_ID"])
        self.assertTrue(binding.exists())
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)

    def test_resume_only_refusals_never_write_binding_or_open_another_cycle(self):
        import artifact_lifecycle
        scenarios = ("route-closed", "campaign-inactive", "resplit", "no-open-cycle",
                     "duplicate-open", "cycle-mismatch", "foreign-owner")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                fixture = OwnerLaunchBindingTest(); fixture.setUp()
                try:
                    route, route_file, launch_env, owner, args = fixture._prepared_owner()
                    binding = P.dispatch_terminal_commit.producer_binding_path(fixture.root, route["route_id"], owner)
                    expected = None
                    record = P.read_cycle_record(fixture.root, launch_env["AGENT_ARTIFACT_CYCLE_ID"])
                    if scenario == "route-closed":
                        outcome = artifact_lifecycle.canonical_outcome_path(fixture.root, route["route_id"])
                        outcome.parent.mkdir(parents=True, exist_ok=True); outcome.write_text("{}")
                        expected = "route-already-closed"
                    elif scenario == "campaign-inactive":
                        campaign = P.read_campaign(fixture.root, record["campaign_id"])
                        campaign["state"] = "abandoned"
                        P._campaign_path(fixture.root, record["campaign_id"], campaign).write_text(json.dumps(campaign))
                        expected = "campaign-not-active"
                    elif scenario == "resplit":
                        (P.producer_dir(fixture.root) / "resplit.lock").write_text("{}")
                        expected = "resplit-in-progress"
                    elif scenario == "no-open-cycle":
                        (P.producer_dir(fixture.root) / "cycles" / f"{record['cycle_id']}.json").unlink()
                        expected = "producer-binding-required"
                    elif scenario == "duplicate-open":
                        duplicate = dict(record, cycle_id="cyc_" + "e" * 32)
                        (P.producer_dir(fixture.root) / "cycles" / f"{duplicate['cycle_id']}.json").write_text(json.dumps(duplicate))
                        expected = "route-cycle-binding-ambiguous"
                    elif scenario == "cycle-mismatch":
                        launch_env = dict(launch_env, AGENT_ARTIFACT_CYCLE_ID="cyc_" + "e" * 32)
                        expected = "producer-binding-mismatch"
                    elif scenario == "foreign-owner":
                        _other_route, other_file = fixture.route("quick", slug="other-owner-route")
                        meta = D.parse_registry_metadata(fixture.jobs.read_text().split("\t", 5)[5])
                        meta.update(route_file=str(other_file), route_id="other", route_hash="sha256:" + "a" * 64)
                        fields = fixture.jobs.read_text().strip().split("\t"); fields[5] = ",".join(f"{k}={v}" for k,v in meta.items())
                        fixture.jobs.write_text("\t".join(fields) + "\n")
                        expected = "route-identity-unverified"
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(fixture.jobs)}):
                        with self.assertRaises(P.ProducerError) as caught:
                            P.bind_owner_launch(args, fixture.jobs, environ=launch_env)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertFalse(binding.exists())
                    expected_cycles = 0 if scenario == "no-open-cycle" else 2 if scenario == "duplicate-open" else 1
                    self.assertEqual(len(P.list_cycle_records(fixture.root)), expected_cycles)
                finally:
                    fixture.doCleanups()

    def test_resume_only_binds_a_closed_cycle_again(self):
        # §45 D-123: a closed cycle is still the route's cycle; the launch binds it, it is not refused.
        route, route_file, launch_env, owner, args = self._prepared_owner()
        record = P.read_cycle_record(self.root, launch_env["AGENT_ARTIFACT_CYCLE_ID"])
        record["state"] = "sealed"
        (P.producer_dir(self.root) / "cycles" / f"{record['cycle_id']}.json").write_text(json.dumps(record))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}):
            result = P.bind_owner_launch(args, self.jobs, environ=launch_env)
        self.assertEqual(result["cycle_id"], record["cycle_id"])
        self.assertTrue(P.dispatch_terminal_commit.producer_binding_path(self.root, route["route_id"], owner).exists())
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)

    def test_nonowner_or_missing_cycle_context_is_a_noop(self):
        route, route_file, launch_env, owner, args = self._prepared_owner()
        binding = P.dispatch_terminal_commit.producer_binding_path(self.root, route["route_id"], owner)
        args.worker_type = "stage"
        self.assertIsNone(P.bind_owner_launch(args, self.jobs, environ=launch_env))
        args.worker_type = "owner"
        self.assertIsNone(P.bind_owner_launch(args, self.jobs, environ={}))
        self.assertFalse(binding.exists())
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)

    def test_resume_only_on_inactive_empty_root_does_not_activate_or_create_cycle(self):
        route, route_file = self.route("quick")
        with self.assertRaises(P.ProducerError) as caught:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="quick",
                    jobs=self.jobs, require_cycle=True, resume_only=True)
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("producer-binding-required", "route-cycle-absent"))
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-empty")
        self.assertEqual(list(P.list_cycle_records(self.root)), [])

    def test_open_cycle_selector_distinguishes_drift_and_duplicate_records(self):
        self.activate()
        route, route_file = self.route()
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        record = P.route_cycle_for(self.root, route)
        self.assertEqual(record["cycle_id"], result["cycle_id"])
        drift = dict(route, route_hash="sha256:" + "f" * 64)
        record_path = P.producer_dir(self.root) / "cycles" / f"{result['cycle_id']}.json"
        original = record_path.read_bytes()
        changed = dict(record, route_hash=drift["route_hash"])
        record_path.write_text(json.dumps(changed), encoding="utf-8")
        checkpoint = P.checkpoint(self.root, route_file=route_file, trigger="explicit")
        self.assertEqual((checkpoint["status"], checkpoint["reason"]), ("skipped", "route-hash-drift"))
        record_path.write_bytes(original)
        duplicate = dict(record, cycle_id="cyc_" + "f" * 32)
        (P.producer_dir(self.root) / "cycles" / f"{duplicate['cycle_id']}.json").write_text(
            json.dumps(duplicate), encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.route_cycle_for(self.root, route)
        self.assertEqual(caught.exception.code, "route-cycle-binding-ambiguous")


class CycleBindingAndIndexOrderTest(ProducerTestBase):
    def test_cycle_binding_records_the_start_time_and_legacy_bindings_still_read(self):
        self.activate()
        _route, _route_file, result = self.begin()
        cycle_dir = Path(result["cycle_dir"])
        record = P.read_cycle_record(self.root, result["cycle_id"])
        binding = json.loads((cycle_dir / ".cycle.json").read_text())
        self.assertEqual(binding["started_on"], record["started_on"])
        self.assertEqual(P.artifact_locator.read_cycle_binding(cycle_dir)["started_on"], record["started_on"])
        self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), cycle_dir)
        # Bindings written before the field existed keep resolving unchanged.
        legacy = {key: value for key, value in binding.items() if key != "started_on"}
        (cycle_dir / ".cycle.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.assertNotIn("started_on", P.artifact_locator.read_cycle_binding(cycle_dir))
        self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), cycle_dir)
        for bad in ({**binding, "started_on": "2026-09-14 09:00"}, {**binding, "sealed_on": binding["started_on"]}):
            (cycle_dir / ".cycle.json").write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(P.artifact_locator.LocatorError):
                P.artifact_locator.read_cycle_binding(cycle_dir)
        with self.assertRaises(P.artifact_locator.LocatorError):
            P.artifact_locator.cycle_binding_bytes(record["campaign_id"], result["cycle_id"], started_on="today")
        # A work date without a clock (resplit/residue cycles) is allowed and stays date-only.
        (cycle_dir / ".cycle.json").write_bytes(P.artifact_locator.cycle_binding_bytes(
            record["campaign_id"], result["cycle_id"], started_on="2026-06-11"))
        self.assertEqual(P.artifact_locator.read_cycle_binding(cycle_dir)["started_on"], "2026-06-11")

    def test_index_markdown_lists_each_campaign_then_its_cycles_in_start_order(self):
        self.activate()

        def open_cycle(slug, key):
            _route, route_file = self.route(slug=slug, gate_source=slug)
            return P.begin(self.root, route_file=route_file, capability="autopilot-code",
                           intensity="direct", campaign_key=key)

        alpha_late = open_cycle("alpha-late", "alpha")
        alpha_early = open_cycle("alpha-early", "alpha")
        beta = open_cycle("beta-only", "beta")
        stamps = {alpha_late["cycle_id"]: "2026-09-14T15:00:00Z",
                  alpha_early["cycle_id"]: "2026-09-14T09:00:00Z",
                  beta["cycle_id"]: "2026-09-13T10:00:00Z"}
        for cycle_id, when in stamps.items():
            path = P.cycle_record_path(self.root, cycle_id)
            record = json.loads(path.read_text(encoding="utf-8"))
            record["started_on"] = when
            path.write_text(json.dumps(record), encoding="utf-8")
        for campaign_id, when in ((alpha_late["campaign_id"], "2026-09-14T08:00:00Z"),
                                  (beta["campaign_id"], "2026-09-13T08:00:00Z")):
            campaign = P.read_campaign(self.root, campaign_id)
            campaign["created_on"] = when
            P._write_campaign(self.root, campaign, exclusive=False)
        P.artifact_locator.rebuild_indexes(self.root)
        markdown = (self.root / "campaigns" / "INDEX.md").read_text(encoding="utf-8")
        order = [line.split("|")[1].strip() for line in markdown.splitlines() if line.startswith("| c")]
        # Older campaign first; within a campaign the same-day cycles follow the clock, not the ID.
        self.assertEqual(order, [beta["campaign_id"], beta["cycle_id"],
                                 alpha_late["campaign_id"], alpha_early["cycle_id"], alpha_late["cycle_id"]])
        self.assertIn("| 2026-09-14T09:00:00Z |", markdown)

    def test_backfill_adds_start_times_to_older_bindings_and_never_estimates(self):
        self.activate()
        route, route_file = self.route(slug="sealed-one", gate_source="sealed-one")
        sealed = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key="backfill")
        self.write_output(sealed)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=sealed["cycle_id"])
        _route, open_file = self.route(slug="open-one", gate_source="open-one")
        opened = P.begin(self.root, route_file=open_file, capability="autopilot-code",
                         intensity="direct", campaign_key="backfill")
        markers = {}
        for result in (sealed, opened):
            marker = Path(result["cycle_dir"]) / ".cycle.json"
            legacy = {k: v for k, v in json.loads(marker.read_text()).items() if k != "started_on"}
            marker.write_text(json.dumps(legacy), encoding="utf-8")
            markers[result["cycle_id"]] = marker
        expected = {cid: P.read_cycle_record(self.root, cid)["started_on"] for cid in markers}

        dry = P.backfill_cycle_bindings(self.root)
        self.assertEqual((dry["status"], dry["counts"]), ("dry-run", {"would-add": 2}))
        self.assertTrue(all("started_on" not in json.loads(m.read_text()) for m in markers.values()))

        applied = P.backfill_cycle_bindings(self.root, apply=True)
        self.assertEqual((applied["status"], applied["counts"]), ("applied", {"added": 2}))
        for cid, marker in markers.items():
            self.assertEqual(P.artifact_locator.read_cycle_binding(marker.parent)["started_on"], expected[cid])
            self.assertEqual(P.artifact_locator.resolve_path(self.root, cid), marker.parent)
        self.assertEqual(P.backfill_cycle_bindings(self.root, apply=True)["counts"], {"present": 2})

        # A sealed cycle whose record lost its time falls back to the manifest; an
        # open cycle with no time anywhere is reported, never guessed from the path.
        for cid in markers:
            path = P.cycle_record_path(self.root, cid)
            record = json.loads(path.read_text()); record.pop("started_on")
            path.write_text(json.dumps(record), encoding="utf-8")
            legacy = {k: v for k, v in json.loads(markers[cid].read_text()).items() if k != "started_on"}
            markers[cid].write_text(json.dumps(legacy), encoding="utf-8")
        by_id = {row["cycle_id"]: row for row in P.backfill_cycle_bindings(self.root)["cycles"]}
        self.assertEqual((by_id[sealed["cycle_id"]]["action"], by_id[sealed["cycle_id"]]["source"],
                          by_id[sealed["cycle_id"]]["started_on"]),
                         ("would-add", "manifest", expected[sealed["cycle_id"]]))
        self.assertEqual((by_id[opened["cycle_id"]]["action"], by_id[opened["cycle_id"]]["source"]),
                         ("missing", None))

        # A W7G resplit cycle records the resplit run in `started_on` and the
        # work's date in `resplit_started_on`; the reader wants the work's date
        # (D-79), in the binding and in INDEX.md alike, never the move time.
        path = P.cycle_record_path(self.root, opened["cycle_id"])
        record = json.loads(path.read_text())
        record["started_on"] = "2026-09-03T15:12:03Z"
        record["resplit_started_on"] = "2026-06-11"
        record["derived_from_cycle_id"] = "cyc_" + "1" * 32
        path.write_text(json.dumps(record), encoding="utf-8")
        row = {r["cycle_id"]: r for r in P.backfill_cycle_bindings(self.root, apply=True)["cycles"]}[opened["cycle_id"]]
        self.assertEqual((row["action"], row["source"], row["started_on"]),
                         ("added", "record:resplit_started_on", "2026-06-11"))
        self.assertEqual(P.artifact_locator.read_cycle_binding(markers[opened["cycle_id"]].parent)["started_on"],
                         "2026-06-11")
        self.assertIn("| 2026-06-11 |", (self.root / "campaigns" / "INDEX.md").read_text(encoding="utf-8"))
        self.assertNotIn("2026-09-03T15:12:03Z", (self.root / "campaigns" / "INDEX.md").read_text(encoding="utf-8"))
        # The fleet-wide resplit stored the folder date as midnight with no
        # `resplit_started_on`; that placeholder clock is dropped the same way.
        record.pop("resplit_started_on")
        record["started_on"] = "2026-07-26T00:00:00Z"
        path.write_text(json.dumps(record), encoding="utf-8")
        legacy = {k: v for k, v in json.loads(markers[opened["cycle_id"]].read_text()).items() if k != "started_on"}
        markers[opened["cycle_id"]].write_text(json.dumps(legacy), encoding="utf-8")
        row = {r["cycle_id"]: r for r in P.backfill_cycle_bindings(self.root)["cycles"]}[opened["cycle_id"]]
        self.assertEqual((row["action"], row["source"], row["started_on"]), ("would-add", "record", "2026-07-26"))
        self.assertEqual(P.artifact_locator.display_started_on(
            {"started_on": "2026-07-26T00:00:00Z"}), "2026-07-26T00:00:00Z")  # a real midnight start stays

        # A binding that already carries a different time is left alone.
        tampered = dict(json.loads(markers[sealed["cycle_id"]].read_text()), started_on="2020-01-01T00:00:00Z")
        markers[sealed["cycle_id"]].write_text(json.dumps(tampered), encoding="utf-8")
        result = P.backfill_cycle_bindings(self.root, apply=True)
        self.assertEqual(result["counts"], {"added": 1, "conflict": 1})
        self.assertEqual(json.loads(markers[sealed["cycle_id"]].read_text())["started_on"], "2020-01-01T00:00:00Z")

    def test_time_recovery_reads_original_mtimes_from_the_retirement_backup(self):
        import hashlib, io, tarfile
        self.activate()
        route, route_file = self.route(slug="migrated-work", gate_source="migrated-work")
        sealed = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key="recovery")
        target = self.write_output(sealed, rel="plans/cycle/plan.md", data=b"plan body\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=sealed["cycle_id"])
        # Pretend this sealed cycle came out of the W7G resplit: date-only start, no clock.
        record_path = P.cycle_record_path(self.root, sealed["cycle_id"])
        record = json.loads(record_path.read_text())
        record.update({"derived_from_cycle_id": "cyc_" + "2" * 32, "started_on": "2026-07-13T00:00:00Z"})
        record_path.write_text(json.dumps(record), encoding="utf-8")
        # A retirement backup of the same root: original bytes under their legacy path, original mtime.
        store = Path(self._tmp.name) / "retirement"
        run_dir = store / ROOT_ID / "20260903T230843Z"
        run_dir.mkdir(parents=True)
        import calendar
        original_mtime = calendar.timegm((2026, 7, 13, 9, 34, 26, 0, 0, 0))  # the legacy file's last write
        with tarfile.open(run_dir / "retired-sources.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo("plans/legacy/plan.md")
            info.size, info.mtime = len(b"plan body\n"), original_mtime
            archive.addfile(info, io.BytesIO(b"plan body\n"))
            other = tarfile.TarInfo("plans/legacy/other.md")
            other.size, other.mtime = 6, original_mtime + 3600
            archive.addfile(other, io.BytesIO(b"other\n"))
        digest = hashlib.sha256(b"plan body\n").hexdigest()
        (run_dir / "retired-manifest.jsonl").write_text(
            json.dumps({"sha256": digest, "size": 10, "source": "plans/legacy/plan.md", "target": "x"}) + "\n"
            + json.dumps({"sha256": hashlib.sha256(b"other\n").hexdigest(), "size": 6,
                          "source": "plans/legacy/other.md", "target": "y"}) + "\n", encoding="utf-8")
        (run_dir / "backup-seal.json").write_text(json.dumps({"archive_sha256": "abc"}), encoding="utf-8")

        dry = P.recover_cycle_times(self.root, backup_store=store)
        self.assertEqual((dry["status"], dry["counts"], dry["backup_runs"]),
                         ("dry-run", {"would-recover": 1}, ["20260903T230843Z"]))
        row = dry["cycles"][0]
        # The folder was named today by `begin`; the recovered date disagreeing with it is reported, not hidden.
        self.assertEqual((row["recovered_started_on"], row["matched"], row["total"], row["folder_date_agrees"]),
                         ("2026-07-13T09:34:26Z", 1, 1, False))
        self.assertNotIn("recovered_started_on", json.loads(record_path.read_text()))
        self.assertTrue((run_dir / "mtime-index.json").is_file())  # listing the archive is cached

        applied = P.recover_cycle_times(self.root, backup_store=store, apply=True)
        self.assertEqual(applied["counts"], {"recovered": 1})
        record = json.loads(record_path.read_text())
        self.assertEqual((record["recovered_started_on"], record["recovered_started_on_source"],
                          record["started_on"], record["recovered_started_on_evidence"]["matched"]),
                         ("2026-07-13T09:34:26Z", P.RECOVERED_SOURCE, "2026-07-13T00:00:00Z", 1))
        journal = Path(applied["journal"])
        self.assertEqual(json.loads(journal.read_text().splitlines()[0])["pre"]["started_on"], "2026-07-13T00:00:00Z")
        self.assertIn("| 2026-07-13T09:34:26Z |", (self.root / "campaigns" / "INDEX.md").read_text(encoding="utf-8"))
        # The campaign row starts with its earliest cycle, not with the day the resplit created it.
        _mapping, view = P.artifact_locator.scan_index(self.root)
        self.assertEqual(view[sealed["campaign_id"]]["started"], "2026-07-13T09:34:26Z")
        self.assertEqual(P.recover_cycle_times(self.root, backup_store=store, apply=True)["counts"], {"already": 1})
        # The recovered clock is what the binding backfill and the display use from now on.
        marker = Path(sealed["cycle_dir"]) / ".cycle.json"
        marker.write_text(json.dumps({k: v for k, v in json.loads(marker.read_text()).items() if k != "started_on"}))
        row = P.backfill_cycle_bindings(self.root, apply=True)["cycles"][0]
        self.assertEqual((row["action"], row["started_on"]), ("added", "2026-07-13T09:34:26Z"))
        # An earliest write *after* the folder's date is a later bulk rewrite, not the
        # start: it is kept as evidence and the display stays date-only.
        record = json.loads(record_path.read_text())
        record["locator"] = "2026-07-01_migrated-work"
        record.pop("recovered_started_on"); record.pop("recovered_earliest_write")
        record_path.write_text(json.dumps(record), encoding="utf-8")
        Path(sealed["cycle_dir"]).rename(Path(sealed["cycle_dir"]).with_name("2026-07-01_migrated-work"))
        row = P.recover_cycle_times(self.root, backup_store=store, apply=True)["cycles"][0]
        self.assertEqual((row["action"], row["display"], row["recovered_started_on"], row["earliest_write"]),
                         ("recovered", "evidence-only", None, "2026-07-13T09:34:26Z"))
        record = json.loads(record_path.read_text())
        self.assertEqual((record.get("recovered_started_on"), record["recovered_earliest_write"]),
                         (None, "2026-07-13T09:34:26Z"))
        self.assertEqual(P.artifact_locator.display_started_on(record), "2026-07-13")
        self.assertEqual(P.recover_cycle_times(self.root, backup_store=store)["counts"], {"already": 1})
        sealed["cycle_dir"] = str(Path(sealed["cycle_dir"]).with_name("2026-07-01_migrated-work"))
        # A cycle whose bytes are not in any backup is reported, never guessed.
        target = Path(sealed["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "plan.md"
        target.write_bytes(b"rewritten after the fact\n")
        record["derived_from_cycle_id"] = "cyc_" + "3" * 32
        manifest_path = Path(sealed["cycle_dir"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for rev in manifest["artifact_revisions"]:
            rev["content_digest"] = "sha256:" + "f" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        record.pop("recovered_started_on", None); record.pop("recovered_earliest_write", None)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(P.recover_cycle_times(self.root, backup_store=store)["counts"], {"no-match": 1})


class RouteLineageBindingTest(ProducerTestBase):
    """SD-155/D-120 (artifact-path-contract A-25): a route's verified lineage
    binds an already-open cycle. Every route in this fixture is published by
    the real builders (`R.compile_route`, `R.build_continuation_route`,
    `R.publish_continuation_route`) -- never a hand-written route file --
    so the hash chain admission relies on is the production one.
    """

    def _root_route(self, slug):
        return R.compile_route(
            "autopilot-code", "dev", "direct", R.ROOT, self.root,
            predicates=["atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
                        "no-artifact-handoff", "no-independent-verifier", "focused-verification"],
            transport=None, inline_reason="atomic-direct",
            tracking="tracked", tracked_gate_evidence=gate_evidence(), slug=slug,
        )

    def test_closed_unused_candidate_keeps_execution_and_unknown_evidence_attached(self):
        begin = self._root_route("unused-candidate-boundaries")
        self._publish_root(begin)
        started = self._begin(begin)
        successor = self._continuation(begin)
        record = P.read_cycle_record(self.root, started["cycle_id"])
        self.assertFalse(P._closed_unexecuted_continuation(self.root, successor))
        successor_file = R.canonical_route_path(self.root, successor["route_id"])
        R.close_route(successor, successor_file, summary="unused candidate")
        self.assertTrue(P._closed_unexecuted_continuation(self.root, successor))
        self.assertTrue(P.cycle_route_admission(self.root, record, begin, finalize=True).allow)

        for metadata_key in ("route_id", "route", "owner_route_id"):
            for launch_started in ("0", "1"):
                for registry_status in ("open", "done"):
                    with self.subTest(metadata_key=metadata_key, launch_started=launch_started,
                                      registry_status=registry_status):
                        self.jobs.write_text(f"2026-10-09\t{registry_status}\t{R.ROOT}\t{R.ROOT}\tchild\t"
                                             f"{metadata_key}={successor['route_id']},attempt_id=att-child,"
                                             f"launch_started={launch_started}\n")
                        self.assertFalse(P._closed_unexecuted_continuation(self.root, successor))
                        self.assertFalse(P.cycle_route_admission(self.root, record, begin, finalize=True).allow)
        self.jobs.write_text("corrupt registry row\n")
        self.assertFalse(P._closed_unexecuted_continuation(self.root, successor))
        self.jobs.write_text("")
        with mock.patch.object(P.artifact_lifecycle._load_capability_route(), "resolve_dangling_registry",
                               side_effect=OSError("unreadable registry")):
            self.assertFalse(P._closed_unexecuted_continuation(self.root, successor))
        markers = R.completion_dir(successor["route_id"], jobs=self.jobs)
        markers.mkdir(parents=True, exist_ok=True)
        (markers / "inline.json").write_text("{}")
        self.assertFalse(P._closed_unexecuted_continuation(self.root, successor))
        (markers / "inline.json").unlink()
        self._continuation(successor)
        self.assertFalse(P._closed_unexecuted_continuation(self.root, successor))

    def _publish_root(self, route):
        path = R.canonical_route_path(self.root, route["route_id"])
        R.write_once(path, route)
        return path

    def _continuation(self, source, *, retint=None, reason="lineage-fixture"):
        """A hash-verified continuation of ``source``, zero prior evidence needed
        (`resume_from_node="inline"` is the route's only, first node)."""
        route = R.build_continuation_route(
            source, resume_from_node="inline", requested_boundary="inline",
            reason=reason, artifact_root=self.root,
        )
        if retint is not None:
            # B'': same verified lineage, sealed `effective_intensity` differs --
            # SD-155 context keys (artifact_root/cwd/capability) never covered
            # intensity, so this still passes lineage verification and only
            # trips D-120 admission's material-input step (A-25.8).
            route = dict(route, effective_intensity=retint)
            route["route_hash"] = R.route_hash(route)
            route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
        path = R.canonical_route_path(self.root, route["route_id"])
        R.publish_continuation_route(route, source, path)
        return route

    def _begin(self, route):
        return P.begin(self.root, route_file=R.canonical_route_path(self.root, route["route_id"]),
                       capability=route["capability"], intensity=route["effective_intensity"])

    def _bindings(self, cycle_id):
        record = P.read_cycle_record(self.root, cycle_id)
        stored = record.get("route_bindings")
        if stored:
            return stored
        # D-120: a record with no `route_bindings` field reads as the begin
        # route alone -- compat view, never written back by a bare read.
        return [{"route_id": record.get("route_id"), "route_hash": record.get("route_hash"),
                "route_file": record.get("route_file"), "basis": "begin",
                "continuation_id": None, "source_route_id": None}]

    def test_inline_binding_uses_admitted_continuation_not_begin_route_equality(self):
        import dispatch_terminal_commit as terminal

        self.activate()
        begin = self._root_route("inline-binding-begin")
        self._publish_root(begin)
        started = self._begin(begin)
        continuation = self._continuation(begin)
        record = P.read_cycle_record(self.root, started["cycle_id"])
        campaign = P.read_campaign(self.root, record["campaign_id"])
        binding = {
            "kind": "inline_producer_binding_v1",
            "artifact_root_id": ROOT_ID,
            "campaign_key": campaign["key"],
            "campaign_id": record["campaign_id"],
            "cycle_id": record["cycle_id"],
            "producer_id": record["producer_id"],
            "route_id": continuation["route_id"],
            "route_hash": continuation["route_hash"],
            "cycle_record_digest": terminal.cycle_identity_digest(record),
            "terminal_marker_digest": "a" * 64,
            "evidence_sha256": "b" * 64,
            "inline_finish_id": "c" * 64,
        }
        slot = self.root / ".runtime/inline-finish/v1" / continuation["route_id"] / "finish.json"
        slot.parent.mkdir(parents=True)
        slot.write_text(json.dumps({
            "schema": "inline_finish_v1", "inline_finish_id": binding["inline_finish_id"],
            "terminal_marker_digest": binding["terminal_marker_digest"], "state": "route-closed",
            "intent": {key: binding[key] for key in (
                "route_id", "route_hash", "artifact_root_id", "campaign_key",
                "campaign_id", "cycle_id", "producer_id", "evidence_sha256")},
        }))
        self.assertNotEqual(record["route_id"], continuation["route_id"])
        P._inline_producer_binding_check(self.root, record["cycle_id"], binding)
        # A second qualifying child makes the continuation's cycle admission
        # ambiguous. The finish binding must defer to D-120 and refuse it,
        # rather than treating the matching finish tuple as cycle ownership.
        sibling = self._continuation(begin, reason="inline-binding-sibling")
        self.assertEqual(
            P.cycle_route_admission(self.root, record, continuation, finalize=True).reason,
            "cycle-route-binding-mismatch:lineage-fork",
        )
        with self.assertRaisesRegex(P.ProducerError, "lineage-fork"):
            P._inline_producer_binding_check(self.root, record["cycle_id"], binding)
        wrong = dict(binding, route_hash=begin["route_hash"])
        with self.assertRaises(P.ProducerError):
            P._inline_producer_binding_check(self.root, record["cycle_id"], wrong)

    def _route_ids(self, cycle_id):
        return [row["route_id"] for row in self._bindings(cycle_id)]

    def _close(self, route):
        route_file = R.canonical_route_path(self.root, route["route_id"])
        self.close(route, route_file)

    def _tree_state(self):
        """Snapshot fixture bytes and symlink targets to prove judgments are pure."""
        state = {}
        for path in sorted(self.root.rglob("*")):
            # D-120 permits disposable lookup caches; purity concerns the
            # records/evidence, not rebuilding these two derived projections.
            if path.name in (".route-children-index.json", ".cycle-routes-index.json"):
                continue
            relative = path.relative_to(self.root).as_posix()
            mode = path.lstat().st_mode
            if path.is_symlink():
                state[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                state[relative] = ("file", path.read_bytes())
            elif path.is_dir():
                state[relative] = ("dir", mode & 0o777)
            else:
                state[relative] = ("other", mode & 0o777)
        return state

    def _check_explicit_cycle(self, cycle_id, route_id, target):
        with mock.patch.dict(os.environ, {
            "AGENT_ARTIFACT_CYCLE_ID": cycle_id,
            "AGENT_ROUTE_ID": route_id,
        }, clear=False):
            os.environ.pop("AGENT_OWNER_ROUTE_ID", None)
            return P.check_write(self.root, target)

    # -- A-25.1 -----------------------------------------------------------
    def test_a25_1_continuation_writes_same_cycle(self):
        a = self._root_route("lineage-a1")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        output = P.require_cycle_output(self.root, Path(begun["cycle_dir"]) / "artifacts" / "x.md",
                                        route_id=b["route_id"])
        self.assertEqual(output, Path(begun["cycle_dir"]) / "artifacts")
        bound = R.bind_continuation_cycle(self.root, a, b)
        self.assertTrue(bound["bound"])
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"], b["route_id"]])

    def test_explicit_cycle_check_write_admits_canonical_continuation_for_owner_and_worker(self):
        self.activate()
        a = self._root_route("explicit-cycle-begin")
        self._publish_root(a)
        begun = self._begin(a)
        continuation = self._continuation(a)
        foreign = self._root_route("explicit-cycle-foreign")
        self._publish_root(foreign)
        target = Path(begun["cycle_dir"]) / "artifacts" / "dev_logs" / "handoff.md"
        before = P.read_cycle_record(self.root, begun["cycle_id"])
        before_bytes = P.cycle_record_path(self.root, begun["cycle_id"]).read_bytes()

        # A worker carries its current route directly.
        with mock.patch.dict(os.environ, {
            "AGENT_ARTIFACT_CYCLE_ID": begun["cycle_id"],
            "AGENT_ROUTE_ID": continuation["route_id"],
        }, clear=False):
            os.environ.pop("AGENT_OWNER_ROUTE_ID", None)
            verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "allow", verdict)

        with mock.patch.dict(os.environ, {
            "AGENT_ARTIFACT_CYCLE_ID": begun["cycle_id"],
            "AGENT_OWNER_ROUTE_ID": continuation["route_id"],
        }, clear=False):
            os.environ.pop("AGENT_ROUTE_ID", None)
            verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "allow", verdict)

        # When both identities are present, the current child route is the
        # authority for its write. An unrelated child route cannot be hidden
        # behind an otherwise-valid owner route.
        with mock.patch.dict(os.environ, {
            "AGENT_ARTIFACT_CYCLE_ID": begun["cycle_id"],
            "AGENT_OWNER_ROUTE_ID": continuation["route_id"],
            "AGENT_ROUTE_ID": foreign["route_id"],
        }, clear=False):
            verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "deny", verdict)
        self.assertEqual(verdict["reason"], "cycle-route-binding-mismatch", verdict)
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"]), before)
        self.assertEqual(P.cycle_record_path(self.root, begun["cycle_id"]).read_bytes(), before_bytes)

        # Wrong cycle, route or output path remains denied by the same checked
        # canonical route and cycle record.
        other = self._root_route("explicit-cycle-other")
        self._publish_root(other)
        other_begun = self._begin(other)
        with self.assertRaises(P.ProducerError) as caught:
            P.require_cycle_output(self.root, target, cycle_id=other_begun["cycle_id"],
                                   route_id=continuation["route_id"])
        self.assertEqual(caught.exception.code, "cycle-route-binding-mismatch")
        with self.assertRaises(P.ProducerError):
            P.require_cycle_output(self.root, self.root / "outside.md",
                                   cycle_id=begun["cycle_id"], route_id=continuation["route_id"])

    def test_explicit_cycle_check_write_denies_unverified_and_nonadmitted_lineage_without_mutation(self):
        self.activate()
        a = self._root_route("explicit-cycle-refusal-begin")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        target = Path(begun["cycle_dir"]) / "artifacts" / "dev_logs" / "result.md"
        route_path = R.canonical_route_path(self.root, b["route_id"])
        original_route = route_path.read_bytes()

        def denied_without_mutation(reason, route_id=None):
            route_id = route_id or b["route_id"]
            before = self._tree_state()
            with self.assertRaises(P.ProducerError) as caught:
                P.require_cycle_output(self.root, target, cycle_id=begun["cycle_id"], route_id=route_id)
            self.assertEqual(caught.exception.code, reason)
            verdict = self._check_explicit_cycle(begun["cycle_id"], route_id, target)
            self.assertEqual((verdict["verdict"], verdict["reason"]), ("deny", reason), verdict)
            self.assertEqual(self._tree_state(), before)

        # The canonical route file is the proof used by check_write. A stale
        # hash, malformed JSON, absence, or a symlink must never fall back to
        # the begin-route equality check when an explicit cycle was selected.
        with self.subTest("tampered-canonical-route"):
            route_path.write_text(json.dumps(dict(b, capability="autopilot-research")), encoding="utf-8")
            denied_without_mutation("route-lineage-unverified")
            route_path.write_bytes(original_route)
        with self.subTest("malformed-canonical-route"):
            route_path.write_text("{malformed", encoding="utf-8")
            denied_without_mutation("route-lineage-unverified")
            route_path.write_bytes(original_route)
        with self.subTest("missing-canonical-route"):
            route_path.unlink()
            denied_without_mutation("cycle-route-binding-mismatch")
            route_path.write_bytes(original_route)
        with self.subTest("symlink-canonical-route"):
            backup = Path(self._tmp.name) / "continuation-route.json"
            backup.write_bytes(original_route)
            route_path.unlink()
            route_path.symlink_to(backup)
            try:
                denied_without_mutation("route-lineage-unverified")
            finally:
                route_path.unlink()
                route_path.write_bytes(original_route)

        with self.subTest("lineage-fork"):
            R.bind_continuation_cycle(self.root, a, b)
            self._continuation(a, reason="explicit-cycle-refusal-sibling")
            denied_without_mutation("cycle-route-binding-mismatch:lineage-fork")

        with self.subTest("changed-material-input"):
            changed = self._continuation(a, retint="quick", reason="explicit-cycle-refusal-retint")
            denied_without_mutation("cycle-route-binding-mismatch:material-input", changed["route_id"])

        # A new, unrelated route cannot claim this cycle, and the route-bound
        # output helper still rejects a path outside that cycle's artifacts.
        foreign = self._root_route("explicit-cycle-refusal-foreign")
        self._publish_root(foreign)
        foreign_begun = self._begin(foreign)
        denied_without_mutation("cycle-route-binding-mismatch", foreign["route_id"])
        before = self._tree_state()
        with self.assertRaisesRegex(P.ProducerError, "artifact-outside-bound-cycle"):
            P.require_cycle_output(self.root, self.root / "outside.md",
                                   cycle_id=begun["cycle_id"], route_id=a["route_id"])
        self.assertEqual(self._tree_state(), before)
        before = self._tree_state()
        verdict = self._check_explicit_cycle(foreign_begun["cycle_id"], b["route_id"], target)
        self.assertEqual((verdict["verdict"], verdict["reason"]),
                         ("deny", "cycle-route-binding-mismatch"), verdict)
        self.assertEqual(self._tree_state(), before)

    def test_explicit_cycle_check_write_preserves_exact_begin_route_compat_fallback(self):
        self.activate()
        route = self._root_route("explicit-cycle-exact-begin")
        self._publish_root(route)
        begun = self._begin(route)
        target = Path(begun["cycle_dir"]) / "artifacts" / "dev_logs" / "begin-route.md"
        route_path = R.canonical_route_path(self.root, route["route_id"])

        # Existing canonical begin-route proof is an exact successful match.
        output = P.require_cycle_output(self.root, target, cycle_id=begun["cycle_id"],
                                        route_id=route["route_id"])
        self.assertEqual(output, Path(begun["cycle_dir"]) / "artifacts")
        verdict = self._check_explicit_cycle(begun["cycle_id"], route["route_id"], target)
        self.assertEqual((verdict["verdict"], verdict["reason"]),
                         ("allow", "open-cycle-artifacts"), verdict)

        # Compatibility applies only to a genuinely absent canonical route
        # file and the exact begin-route identity. It never authorizes a
        # missing continuation route (covered in the refusal matrix above).
        original = route_path.read_bytes()
        route_path.unlink()
        try:
            before = self._tree_state()
            output = P.require_cycle_output(self.root, target, cycle_id=begun["cycle_id"],
                                            route_id=route["route_id"])
            self.assertEqual(output, Path(begun["cycle_dir"]) / "artifacts")
            with mock.patch.dict(os.environ, {
                "AGENT_ARTIFACT_CYCLE_ID": begun["cycle_id"],
                "AGENT_ROUTE_ID": route["route_id"],
            }, clear=False):
                os.environ.pop("AGENT_OWNER_ROUTE_ID", None)
                verdict = P.check_write(self.root, target)
            self.assertEqual((verdict["verdict"], verdict["reason"]),
                             ("allow", "open-cycle-artifacts"), verdict)
            self.assertEqual(self._tree_state(), before)
        finally:
            route_path.write_bytes(original)

    def test_explicit_cycle_check_write_binds_sealed_cycles_and_refuses_folderless_ones_without_mutation(self):
        for final_state in ("sealed", "abandoned"):
            with self.subTest(final_state=final_state):
                fixture = RouteLineageBindingTest()
                fixture.setUp()
                try:
                    fixture.activate()
                    route = fixture._root_route("explicit-cycle-closed-" + final_state)
                    fixture._publish_root(route)
                    begun = fixture._begin(route)
                    target = Path(begun["cycle_dir"]) / "artifacts" / "dev_logs" / "late.md"
                    if final_state == "sealed":
                        existing = Path(begun["cycle_dir"]) / "artifacts" / "plans" / "first.md"
                        existing.parent.mkdir(parents=True, exist_ok=True)
                        existing.write_bytes(b"first\n")
                        fixture._close(route)
                        P.finalize(fixture.root, cycle_id=begun["cycle_id"])
                    else:
                        P.finalize(fixture.root, cycle_id=begun["cycle_id"], state="abandoned",
                                   abandon_reason="operator-decision")
                    before = fixture._tree_state()
                    if final_state == "sealed":
                        # §45 D-123: a closed cycle with its folder takes the route's writes.
                        output = P.require_cycle_output(fixture.root, target, cycle_id=begun["cycle_id"],
                                                        route_id=route["route_id"])
                        self.assertEqual(output, Path(begun["cycle_dir"]) / "artifacts")
                        verdict = fixture._check_explicit_cycle(begun["cycle_id"], route["route_id"], target)
                        self.assertEqual((verdict["verdict"], verdict["reason"]),
                                         ("allow", "open-cycle-artifacts"), verdict)
                    else:
                        # A zero-output close removed the folder: nothing is left to bind to.
                        with self.assertRaises(P.ProducerError) as caught:
                            P.require_cycle_output(fixture.root, target, cycle_id=begun["cycle_id"],
                                                   route_id=route["route_id"])
                        self.assertEqual(caught.exception.code, "cycle-route-binding-mismatch")
                        verdict = fixture._check_explicit_cycle(begun["cycle_id"], route["route_id"], target)
                        self.assertEqual(verdict["verdict"], "deny", verdict)
                    self.assertEqual(fixture._tree_state(), before)
                finally:
                    fixture.doCleanups()

    def test_explicit_cycle_check_write_cannot_select_one_cycle_from_ambiguous_lineage(self):
        self.activate()
        route = self._root_route("explicit-cycle-ambiguous")
        self._publish_root(route)
        begun = self._begin(route)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        duplicate = dict(record, cycle_id="cyc_" + "d" * 32)
        duplicate_path = P.producer_dir(self.root) / "cycles" / f"{duplicate['cycle_id']}.json"
        duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
        target = Path(begun["cycle_dir"]) / "artifacts" / "dev_logs" / "ambiguous.md"
        before = self._tree_state()
        with self.assertRaises(P.ProducerError) as caught:
            P.require_cycle_output(self.root, target, cycle_id=begun["cycle_id"],
                                   route_id=route["route_id"])
        self.assertEqual(caught.exception.code, "route-cycle-binding-ambiguous")
        self.assertEqual(self._tree_state(), before)
        with mock.patch.dict(os.environ, {
            "AGENT_ARTIFACT_CYCLE_ID": begun["cycle_id"],
            "AGENT_ROUTE_ID": route["route_id"],
        }, clear=False):
            os.environ.pop("AGENT_OWNER_ROUTE_ID", None)
            verdict = P.check_write(self.root, target)
        self.assertEqual((verdict["verdict"], verdict["reason"]),
                         ("deny", "route-cycle-binding-ambiguous"), verdict)
        self.assertEqual(self._tree_state(), before)

    # -- A-25.2 -------------------------------------------------------------
    def test_a25_2_foreign_route_refused(self):
        a = self._root_route("lineage-a2")
        self._publish_root(a)
        begun = self._begin(a)
        f = self._root_route("lineage-f2")  # not a's continuation at all
        self._publish_root(f)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        admission = P.cycle_route_admission(self.root, record, f)
        self.assertFalse(admission.allow)
        self.assertEqual(admission.reason, "cycle-route-binding-mismatch")
        # A pure judgment call writes nothing: the record never grew a
        # `route_bindings` field at all.
        self.assertNotIn("route_bindings", P.read_cycle_record(self.root, begun["cycle_id"]))

    # -- A-25.3 ---------------------------------------------------------
    def test_a25_3_forged_lineage_refused(self):
        a = self._root_route("lineage-a3")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        b_path = R.canonical_route_path(self.root, b["route_id"])

        with self.subTest("tampered-bytes"):
            tampered = dict(b, capability="autopilot-research")  # route_hash now stale
            b_path.write_text(json.dumps(tampered), encoding="utf-8")
            reloaded = json.loads(b_path.read_text(encoding="utf-8"))
            admission = P.cycle_route_admission(self.root, record, reloaded)
            self.assertEqual(admission.reason, "route-lineage-unverified")
            b_path.write_text(json.dumps(b), encoding="utf-8")  # restore

        with self.subTest("source-route-hash-mismatch"):
            forged = dict(b, source_route_hash="sha256:" + "0" * 64)
            forged["route_hash"] = R.route_hash(forged)
            forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
            forged_path = R.canonical_route_path(self.root, forged["route_id"])
            R.write_once(forged_path, forged)
            admission = P.cycle_route_admission(self.root, record, forged)
            self.assertEqual(admission.reason, "route-lineage-unverified")

        with self.subTest("record-begin-hash-only-differs"):
            drifted = dict(record, route_hash="sha256:" + "1" * 64)
            admission = P.cycle_route_admission(self.root, drifted, b)
            self.assertEqual(admission.reason, "route-hash-drift")

    def test_manifest_route_resolution_rejects_hash_mismatch_and_symlink_kind(self):
        source = self._root_route("lineage-manifest-route")
        source_path = self._publish_root(source)
        begun = self._begin(source)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        root_identity = L.read_root_identity(self.root)
        document = {"artifact_root_id": root_identity.artifact_root_id,
            "cycle": {"cycle_id": begun["cycle_id"]},
            "routes": [{"artifact_root_id": root_identity.artifact_root_id,
                "route_id": source["route_id"], "route_hash": source["route_hash"]}]}
        drifted = json.loads(json.dumps(document))
        drifted["routes"][0]["route_hash"] = "sha256:" + "f" * 64
        with self.assertRaises(P.ProducerError) as caught:
            P.resolve_cycle_manifest_route(self.root, record, drifted)
        self.assertEqual(caught.exception.code, "completion-route-hash-mismatch")

        backup = source_path.with_suffix(".json.backup")
        source_path.replace(backup)
        try:
            source_path.symlink_to(backup)
            with self.assertRaises(P.ProducerError) as caught:
                P.resolve_cycle_manifest_route(self.root, record, document)
            self.assertEqual(caught.exception.code, "route-lineage-unverified")
        finally:
            source_path.unlink(missing_ok=True)
            backup.replace(source_path)

        child = self._continuation(source)
        child_document = {"artifact_root_id": root_identity.artifact_root_id,
            "cycle": {"cycle_id": begun["cycle_id"]},
            "routes": [{"artifact_root_id": root_identity.artifact_root_id,
                "route_id": child["route_id"], "route_hash": child["route_hash"]}]}
        source_backup = source_path.with_suffix(".json.parent-backup")
        source_path.replace(source_backup)
        try:
            source_path.symlink_to(source_backup)
            with self.assertRaises(P.ProducerError) as caught:
                P.resolve_cycle_manifest_route(self.root, record, child_document)
            self.assertEqual(caught.exception.code, "route-lineage-unverified")
        finally:
            source_path.unlink(missing_ok=True)
            source_backup.replace(source_path)
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"]), record)

    # -- A-25.4 -----------------------------------------------------------
    def test_a25_4_owner_begin_on_continuation_rebinds(self):
        a = self._root_route("lineage-a4")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        resumed = P.begin(self.root, route_file=R.canonical_route_path(self.root, b["route_id"]),
                          capability=b["capability"], intensity=b["effective_intensity"],
                          title="후속 작업의 한국어 제목")
        self.assertEqual((resumed["status"], resumed.get("rebound"), resumed["cycle_id"]),
                         ("resumed", True, begun["cycle_id"]))
        self.assertEqual(P.list_cycle_records(self.root).__len__(), 1)  # no new cycle
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"], b["route_id"]])
        record_before = P.read_cycle_record(self.root, begun["cycle_id"])
        self.assertEqual(record_before["title"], "후속 작업의 한국어 제목")
        self.assertTrue(resumed["title_updated"])
        P.require_cycle_output(self.root, Path(begun["cycle_dir"]) / "artifacts" / "y.md", route_id=b["route_id"])
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"]), record_before)

    # -- D-120 (P2 결함 #2): worker `begin --node` on a rebind judges only ----
    def test_worker_begin_node_on_a_rebind_writes_no_audit_record(self):
        """D-120 explicitly limits the rebind audit write to an OWNER begin
        (`node_id is None`); a worker's `begin --node` on the same continuation
        must rebind (so `require_cycle_output` still resolves against the
        shared cycle) without ever appending to `route_bindings`. P2's own
        handoff flagged this as checked only by code review, never by a test
        -- this fixture is the missing proof."""
        a = self._root_route("lineage-a4b")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        record_before = P.read_cycle_record(self.root, begun["cycle_id"])
        node_id = b["nodes"][0]["id"]
        resumed = P.begin(self.root, route_file=R.canonical_route_path(self.root, b["route_id"]),
                          capability=b["capability"], intensity=b["effective_intensity"], node_id=node_id)
        self.assertEqual((resumed["status"], resumed.get("rebound"), resumed["cycle_id"]),
                         ("resumed", True, begun["cycle_id"]))
        self.assertEqual(P.list_cycle_records(self.root).__len__(), 1)  # no new cycle
        # No audit write at all -- record is byte-identical to before, and the
        # compat view (no `route_bindings` field) still names only `a`.
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"]), record_before)
        self.assertNotIn("route_bindings", P.read_cycle_record(self.root, begun["cycle_id"]))
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"]])

    # -- A-25.5 -----------------------------------------------------------
    def test_a25_5_finalize_seals_last_route_without_schema_change(self):
        a = self._root_route("lineage-a5")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        P.begin(self.root, route_file=R.canonical_route_path(self.root, b["route_id"]),
               capability=b["capability"], intensity=b["effective_intensity"])
        target = Path(begun["cycle_dir"]) / "artifacts" / "plan.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"body\n")
        self._close(b)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        expected_digest = P._digest(P._canonical({
            "route_id": record["route_id"], "route_hash": record["route_hash"],
            "capability": record["capability"], "intensity": record["intensity"],
        }))
        sealed = P.finalize(self.root, cycle_id=begun["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        manifest = json.loads((Path(begun["cycle_dir"]) / "manifest.json").read_text())
        report = m.validate(manifest)
        self.assertTrue(report.ok, report.violations)
        self.assertEqual(len(manifest["routes"]), 1)
        self.assertEqual(manifest["routes"][0]["route_id"], b["route_id"])
        self.assertEqual(manifest["cycle"]["input_digest"], expected_digest)
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"], b["route_id"]])

    # -- A-25.6 -----------------------------------------------------------
    def test_a25_6_two_generations_and_gap_append(self):
        a = self._root_route("lineage-a6")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        d = self._continuation(b)
        P.begin(self.root, route_file=R.canonical_route_path(self.root, d["route_id"]),
               capability=d["capability"], intensity=d["effective_intensity"])
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"], b["route_id"], d["route_id"]])
        # Drop the audit trail back to `[A]` (as if B's bind never landed), then
        # confirm D's owner begin repairs it to `[A, B, D]` in one call.
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        P._write_cycle_record(self.root, {**record, "route_bindings": [record["route_bindings"][0]]},
                              exclusive=False)
        P.begin(self.root, route_file=R.canonical_route_path(self.root, d["route_id"]),
               capability=d["capability"], intensity=d["effective_intensity"])
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"], b["route_id"], d["route_id"]])

    # -- A-25.7 -----------------------------------------------------------
    def test_a25_7_sealed_cycle_admits_its_lineage_again(self):
        a = self._root_route("lineage-a7")
        self._publish_root(a)
        begun = self._begin(a)
        target = Path(begun["cycle_dir"]) / "artifacts" / "plan.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"body\n")
        self._close(a)
        P.finalize(self.root, cycle_id=begun["cycle_id"])
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        admission = P.cycle_route_admission(self.root, record, a)
        self.assertTrue(admission.allow, admission)
        self.assertEqual(P.route_cycle_for(self.root, a)["cycle_id"], begun["cycle_id"])

    # -- A-25.8 ----------------------------------------------------------
    def test_a25_8_material_input_change_refused(self):
        a = self._root_route("lineage-a8")
        self._publish_root(a)
        begun = self._begin(a)
        bpp = self._continuation(a, retint="quick")
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        admission = P.cycle_route_admission(self.root, record, bpp)
        self.assertEqual(admission.reason, "cycle-route-binding-mismatch:material-input")
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"]])  # write 0

    # -- A-25.9 (full eleven-case matrix, §42.1 item 9) --------------------
    def test_a25_9_audit_record_tamper_changes_no_decision(self):
        """Tampering the audit record (`route_bindings[]`) never changes any
        admission judgment -- judgment reads only the sealed route files and
        the record's begin `route_id`/`route_hash`/state/capability/intensity,
        never `route_bindings` itself. The only thing tampering can move is
        the *rewrite*: the next recording call (an owner begin or a finalize,
        both funnelled through `_bind_cycle_route_locked`) restores
        `[A, B, D]` and reports `route-binding-record-drift` unless the
        tampered list was already a valid prefix of the correct chain (case
        (f) -- "not yet bound further" is not evidence of tampering)."""
        a = self._root_route("lineage-a9")
        self._publish_root(a)
        begun = self._begin(a)
        cid = begun["cycle_id"]
        b = self._continuation(a)
        P.begin(self.root, route_file=R.canonical_route_path(self.root, b["route_id"]),
               capability=b["capability"], intensity=b["effective_intensity"])
        d = self._continuation(b)
        P.begin(self.root, route_file=R.canonical_route_path(self.root, d["route_id"]),
               capability=d["capability"], intensity=d["effective_intensity"])
        f = self._root_route("lineage-f9")
        self._publish_root(f)
        bpp = self._continuation(a, retint="quick", reason="lineage-fixture-a9-bpp")
        self.assertEqual(self._route_ids(cid), [a["route_id"], b["route_id"], d["route_id"]])

        base = P.read_cycle_record(self.root, cid)["route_bindings"]
        a_e, b_e, d_e = (dict(e) for e in base)
        f_e = P._route_binding_entry(f, self.root, is_begin=False)
        bpp_e = P._route_binding_entry(bpp, self.root, is_begin=False)

        # (a)-(k) exactly as artifact-path-contract §42.1 item 9 lists them.
        cases = {
            "a": [a_e, b_e, d_e, f_e],                                    # append F at the end
            "b": [a_e, f_e, b_e, d_e],                                    # insert F in the middle
            "c": [a_e, dict(b_e, route_hash="sha256:" + "2" * 64), d_e],  # B's route_hash changed
            "d": [a_e, dict(b_e, route_id=f["route_id"]), d_e],           # B's route_id -> F
            "e": [a_e, d_e, b_e],                                         # reorder
            "f": [a_e, b_e],                                              # drop the tail ([A, B])
            "g": [a_e, d_e],                                              # drop the middle ([A, D])
            "h": [a_e, dict(b_e), dict(b_e), d_e],                        # duplicate B
            "i": [a_e, bpp_e, d_e],                                       # B -> B''s sealed values
            "j": [a_e, b_e, dict(d_e, basis="owner-correction")],         # basis changed
            "k": [],                                                      # route_bindings wiped empty
        }
        for letter, bindings in cases.items():
            with self.subTest(case=letter):
                record = P.read_cycle_record(self.root, cid)
                tampered = dict(record, route_bindings=[dict(e) for e in bindings])
                P._write_cycle_record(self.root, tampered, exclusive=False)
                tampered = P.read_cycle_record(self.root, cid)  # includes the normal writer's observation digest

                for route in (a, b, d):
                    self.assertTrue(P.cycle_route_admission(self.root, tampered, route).allow)
                self.assertEqual(P.cycle_route_admission(self.root, tampered, f).reason,
                                 "cycle-route-binding-mismatch")
                self.assertEqual(P.cycle_route_admission(self.root, tampered, bpp).reason,
                                 "cycle-route-binding-mismatch:material-input")
                self.assertEqual(P.cycle_route_admission(self.root, tampered, b, finalize=True).reason,
                                 "cycle-route-binding-mismatch:superseded-route")
                self.assertTrue(P.cycle_route_admission(self.root, tampered, d, finalize=True).allow)
                # Refused writes (F, B'') never touch the tampered bytes.
                self.assertEqual(P.read_cycle_record(self.root, cid), tampered)

                result = P.bind_cycle_route(self.root, cid, d)
                self.assertTrue(result["written"])
                if letter == "f":
                    self.assertIsNone(result["advisory"])
                else:
                    self.assertIsNotNone(result["advisory"])
                    self.assertTrue(result["advisory"].startswith("route-binding-record-drift"))
                self.assertEqual(self._route_ids(cid), [a["route_id"], b["route_id"], d["route_id"]])

    # -- A-25.9's D-finalize leg: a tampered record still seals routes[]=D --
    def test_a25_9_finalize_seals_d_despite_tampered_audit_record(self):
        a = self._root_route("lineage-a9fin")
        self._publish_root(a)
        begun = self._begin(a)
        cid = begun["cycle_id"]
        b = self._continuation(a)
        P.begin(self.root, route_file=R.canonical_route_path(self.root, b["route_id"]),
               capability=b["capability"], intensity=b["effective_intensity"])
        d = self._continuation(b)
        d_begin = P.begin(self.root, route_file=R.canonical_route_path(self.root, d["route_id"]),
                          capability=d["capability"], intensity=d["effective_intensity"])
        target = Path(d_begin["cycle_dir"]) / "artifacts" / "plan.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"body\n")
        self._close(d)
        record = P.read_cycle_record(self.root, cid)
        a_e, b_e, d_e = record["route_bindings"]
        # (h)-shaped tamper: duplicate B ahead of D.
        tampered = dict(record, route_bindings=[a_e, dict(b_e), dict(b_e), d_e])
        P._write_cycle_record(self.root, tampered, exclusive=False)
        sealed = P.finalize(self.root, cycle_id=cid)
        self.assertEqual(sealed["status"], "sealed")
        manifest = json.loads((Path(d_begin["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(len(manifest["routes"]), 1)
        self.assertEqual(manifest["routes"][0]["route_id"], d["route_id"])
        self.assertEqual(self._route_ids(cid), [a["route_id"], b["route_id"], d["route_id"]])

    # -- A-25.10 --------------------------------------------------------
    def test_a25_10_sibling_fork_is_symmetric(self):
        a = self._root_route("lineage-a10")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        R.bind_continuation_cycle(self.root, a, b)
        bprime = self._continuation(a, reason="lineage-fixture-prime")  # a second, sibling continuation of A
        fork_bind = R.bind_continuation_cycle(self.root, a, bprime)
        self.assertFalse(fork_bind["bound"])
        self.assertEqual(fork_bind["advisory"], "cycle-lineage-fork")
        self.assertEqual(self._route_ids(begun["cycle_id"]), [a["route_id"], b["route_id"]])  # no write
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        self.assertEqual(P.cycle_route_admission(self.root, record, b).reason,
                         "cycle-route-binding-mismatch:lineage-fork")
        self.assertEqual(P.cycle_route_admission(self.root, record, bprime).reason,
                         "cycle-route-binding-mismatch:lineage-fork")
        self.assertEqual(P.cycle_route_admission(self.root, record, b, finalize=True).reason,
                         "cycle-route-binding-mismatch:lineage-fork")
        self.assertTrue(P.cycle_route_admission(self.root, record, a).allow)  # A's own late write still allows

    # -- A-25.11 ----------------------------------------------------------
    def test_a25_11_superseded_route_cannot_seal(self):
        a = self._root_route("lineage-a11")
        self._publish_root(a)
        begun = self._begin(a)
        b = self._continuation(a)
        R.bind_continuation_cycle(self.root, a, b)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        admission = P.cycle_route_admission(self.root, record, a, finalize=True)
        self.assertEqual(admission.reason, "cycle-route-binding-mismatch:superseded-route")
        self.assertTrue(P.cycle_route_admission(self.root, record, a).allow)  # A's late write still allows

    # -- A-SD156-2 ---------------------------------------------------------
    def test_a_sd156_2_commit_needs_no_continuation(self):
        a = self._root_route("lineage-sd156-2")
        self._publish_root(a)
        begun = self._begin(a)
        output = P.require_cycle_output(self.root, Path(begun["cycle_dir"]) / "artifacts" / "report.md",
                                        route_id=a["route_id"])
        self.assertEqual(output, Path(begun["cycle_dir"]) / "artifacts")
        self.assertEqual(len(self._bindings(begun["cycle_id"])), 1)

    # -- closed lineage handover (auto-close leftovers) ---------------------
    def _handover_shape(self, a, b, *, bind=False):
        """A cycle C1 begun at ``a``, then a second cycle C2 begun at the
        continuation ``b`` while C1 was briefly not open (the shape a real
        root showed: a resumed route began a fresh cycle inside C1's lineage). C1 is open again."""
        first = self._begin(a)
        if bind:
            self.assertTrue(R.bind_continuation_cycle(self.root, a, b)["bound"])
        record = P.read_cycle_record(self.root, first["cycle_id"])
        P._write_cycle_record(self.root, dict(record, state="abandoned"), exclusive=False)
        second = self._begin(b)
        self.assertEqual(second["status"], "begun")
        P._write_cycle_record(self.root, record, exclusive=False)
        return first, second

    def _seal_abandoned(self, cycle, rel="plans/handover/note.md"):
        self.write_output(cycle, rel, b"leftover\n")
        return P.finalize(self.root, cycle_id=cycle["cycle_id"], state="abandoned",
                          abandon_reason="route-unrecoverable")

    def _manifest_route_ids(self, sealed):
        document = json.loads(Path(sealed["manifest_path"]).read_text(encoding="utf-8"))
        return [row["route_id"] for row in document["routes"]]

    def test_closed_lineage_cycle_seals_on_its_own_route(self):
        a = self._root_route("handover-own")
        self._publish_root(a)
        b = self._continuation(a)
        c = self._continuation(b)
        first, second = self._handover_shape(a, b)
        first_record = P.read_cycle_record(self.root, first["cycle_id"])
        second_record = P.read_cycle_record(self.root, second["cycle_id"])
        # The lineage is live: the D-120 answers are the ones a running route gets.
        with self.assertRaises(P.ProducerError) as caught:
            P.route_cycle_for(self.root, b)
        self.assertEqual(caught.exception.code, "route-cycle-binding-ambiguous")
        self.assertEqual(P.cycle_route_admission(self.root, first_record, a, finalize=True).reason,
                         "cycle-route-binding-mismatch:superseded-route")
        self.assertEqual(P.closed_lineage_handover(self.root, first_record),
                         P.LineageHandover(False, frozenset()))
        self.assertEqual(P._finalize_route(self.root, first_record)["route_id"], c["route_id"])
        for route in (a, b, c):
            self._close(route)
        # Closed: each cycle owns the stretch up to the next cycle's begin route.
        handover = P.closed_lineage_handover(self.root, P.read_cycle_record(self.root, first["cycle_id"]))
        self.assertEqual(handover, P.LineageHandover(True, frozenset({b["route_id"]})))
        self.assertEqual(P._finalize_route(self.root, first_record)["route_id"], a["route_id"])
        self.assertEqual(P._finalize_route(self.root, second_record)["route_id"], c["route_id"])
        sealed_first = self._seal_abandoned(first)
        sealed_second = self._seal_abandoned(second)
        self.assertEqual(self._manifest_route_ids(sealed_first), [a["route_id"]])
        self.assertEqual(self._manifest_route_ids(sealed_second), [c["route_id"]])
        # Sealed replay re-judges the manifest route with the same rule.
        for cycle, sealed in ((first, sealed_first), (second, sealed_second)):
            record = P.read_cycle_record(self.root, cycle["cycle_id"])
            document = json.loads(Path(sealed["manifest_path"]).read_text(encoding="utf-8"))
            P.resolve_cycle_manifest_route(self.root, record, document)
            self.assertEqual(P.finalize(self.root, cycle_id=cycle["cycle_id"], state="abandoned",
                                        abandon_reason="route-unrecoverable")["storage_state"], "sealed")

    def test_closed_lineage_fork_seals_each_branch(self):
        a = self._root_route("handover-fork")
        self._publish_root(a)
        x = self._continuation(a, reason="fork-x")
        y = self._continuation(a, reason="fork-y")
        first = self._begin(a)
        record = P.read_cycle_record(self.root, first["cycle_id"])
        P._write_cycle_record(self.root, dict(record, state="abandoned"), exclusive=False)
        second = self._begin(y)
        P._write_cycle_record(self.root, record, exclusive=False)
        first_record = P.read_cycle_record(self.root, first["cycle_id"])
        # Live: the fork stays a refusal.
        with self.assertRaisesRegex(P.ProducerError, "lineage-fork"):
            P._finalize_route(self.root, first_record)
        self.assertEqual(P.cycle_route_admission(self.root, first_record, x, finalize=True).reason,
                         "cycle-route-binding-mismatch:lineage-fork")
        for route in (a, x, y):
            self._close(route)
        self.assertEqual(P._finalize_route(self.root, first_record)["route_id"], x["route_id"])
        sealed_first = self._seal_abandoned(first)
        sealed_second = self._seal_abandoned(second)
        self.assertEqual(self._manifest_route_ids(sealed_first), [x["route_id"]])
        self.assertEqual(self._manifest_route_ids(sealed_second), [y["route_id"]])

    def test_index_duplicate_reproduced_and_resolved(self):
        a = self._root_route("handover-index")
        self._publish_root(a)
        b = self._continuation(a)
        first, second = self._handover_shape(a, b, bind=True)
        first_record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertEqual([row["route_id"] for row in first_record["route_bindings"]],
                         [a["route_id"], b["route_id"]])
        for route in (a, b):
            self._close(route)
        sealed_second = self._seal_abandoned(second)
        self.assertEqual(self._manifest_route_ids(sealed_second), [b["route_id"]])
        # Before the fix the leaf B was chosen for the first cycle too. Build that
        # document and let the index judge it: the key (root, B) is C2's.
        directory = P.cycle_dir(self.root, first_record["campaign_id"], first["cycle_id"], first_record)
        self.write_output(first, "plans/handover/note.md", b"leftover\n")
        rows, violations = P._enumerate_output(directory)
        self.assertEqual(violations, [])
        document = P.build_manifest(self.root, first_record, b, rows, state="abandoned",
                                    primary=None, allow_open_route=False,
                                    allocator=P.artifact_identity.IdAllocator(), now=None,
                                    abandon_reason="route-unrecoverable", support_locators=(), reserved=None)
        index = adm.load_index(self.root)
        identity = P.artifact_lifecycle.read_root_identity(self.root)
        report = P.artifact_index.check(
            index, document, idempotency_key=first["cycle_id"],
            manifest_digest=P.artifact_manifest.manifest_digest(document),
            repository_id=identity.repository_id if identity else None)
        self.assertIn("index-route-composite-duplicate", [v.code for v in report.violations])
        root_id = document["artifact_root_id"]
        self.assertEqual(index.routes[root_id][b["route_id"]]["cycle_id"], second["cycle_id"])
        # After: C1 seals on A, both keys coexist.
        sealed_first = self._seal_abandoned(first)
        self.assertEqual(self._manifest_route_ids(sealed_first), [a["route_id"]])
        index = adm.load_index(self.root)
        self.assertEqual(index.routes[root_id][a["route_id"]]["cycle_id"], first["cycle_id"])
        self.assertEqual(index.routes[root_id][b["route_id"]]["cycle_id"], second["cycle_id"])

    def test_index_key_already_held_still_rejects(self):
        a = self._root_route("handover-index-held")
        self._publish_root(a)
        b = self._continuation(a)
        first, second = self._handover_shape(a, b)
        for route in (a, b):
            self._close(route)
        # A foreign sealed cycle already holds the key C1 would claim (A): the
        # invariant is not weakened, the seal is still refused.
        index = adm.load_index(self.root)
        root_id = next(iter(adm.load_index(self.root).routes), None) or P.artifact_lifecycle.read_root_identity(
            self.root).artifact_root_id
        routes = {key: dict(value) for key, value in index.routes.items()}
        routes.setdefault(root_id, {})[a["route_id"]] = {"cycle_id": "cyc_" + "f" * 32, "route_hash": None}
        held = dataclasses.replace(index, routes=routes)
        with mock.patch.object(adm, "load_index", return_value=held):
            with self.assertRaisesRegex(P.ProducerError, "index-rejected"):
                self._seal_abandoned(first)
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["state"], "open")

    def test_seal_excludes_symlinks_in_every_state_without_a_flag(self):
        a = self._root_route("handover-symlinks")
        self._publish_root(a)
        begun = self._begin(a)
        self._close(a)
        target_dir = Path(begun["cycle_dir"]) / "artifacts" / "plans" / "links"
        target_dir.mkdir(parents=True)
        (target_dir / "real.md").write_bytes(b"real\n")
        locked = Path(self._tmp.name) / "locked.txt"
        locked.write_bytes(b"secret\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        (target_dir / "relative.md").symlink_to("real.md")
        (target_dir / "locked.md").symlink_to(locked)
        (target_dir / "dangling.md").symlink_to("missing.md")
        links = {"artifacts/plans/links/relative.md": "real.md",
                 "artifacts/plans/links/locked.md": str(locked),
                 "artifacts/plans/links/dangling.md": "missing.md"}
        cycle_id = begun["cycle_id"]
        # §45 D-123: a link is left out of the manifest by the one inclusion rule;
        # neither a completed nor an abandoned close needs a flag for it.
        sealed = P.finalize(self.root, cycle_id=cycle_id, state="abandoned",
                            abandon_reason="route-unrecoverable")
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sorted(sealed["excluded_symlinks"]), sorted(links))
        document = json.loads(Path(sealed["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual([row["locator"]["path"] for row in document["artifact_revisions"]],
                         ["artifacts/plans/links/real.md"])
        self.assertEqual(sorted(P.read_cycle_record(self.root, cycle_id)["excluded_symlinks"]), sorted(links))
        for rel, value in links.items():
            self.assertEqual(os.readlink(Path(begun["cycle_dir"]) / rel), value)
        self.assertEqual(locked.stat().st_mode & 0o777, 0)
        locked.chmod(0o600)
        self.assertEqual(locked.read_bytes(), b"secret\n")

    def test_excluded_symlinks_survive_a_crash_after_the_manifest_is_published(self):
        a = self._root_route("handover-symlink-crash")
        self._publish_root(a)
        begun = self._begin(a)
        self._close(a)
        target_dir = Path(begun["cycle_dir"]) / "artifacts" / "plans" / "links"
        target_dir.mkdir(parents=True)
        (target_dir / "real.md").write_bytes(b"real\n")
        (target_dir / "relative.md").symlink_to("real.md")
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=begun["cycle_id"], state="abandoned",
                       abandon_reason="route-unrecoverable", exclude_symlinks=True, crash_after_manifest=True)
        P.recover(self.root)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(record["excluded_symlinks"], ["artifacts/plans/links/relative.md"])

    def test_closed_lineage_handover_reports_open_tree(self):
        a = self._root_route("handover-report")
        self._publish_root(a)
        b = self._continuation(a)
        first, second = self._handover_shape(a, b)
        first_record = P.read_cycle_record(self.root, first["cycle_id"])
        second_record = P.read_cycle_record(self.root, second["cycle_id"])
        self._close(a)
        # B is still open: a live tree hands nothing over.
        self.assertEqual(P.closed_lineage_handover(self.root, first_record), P.LineageHandover(False, frozenset()))
        self._close(b)
        self.assertEqual(P.closed_lineage_handover(self.root, first_record),
                         P.LineageHandover(True, frozenset({b["route_id"]})))
        self.assertEqual(P.closed_lineage_handover(self.root, second_record),
                         P.LineageHandover(True, frozenset()))
        # No other cycle begins inside the tree: closed, nothing handed over.
        lone = self._root_route("handover-report-lone")
        self._publish_root(lone)
        lone_child = self._continuation(lone)
        lone_cycle = self._begin(lone)
        lone_record = P.read_cycle_record(self.root, lone_cycle["cycle_id"])
        self._close(lone)
        self.assertEqual(P.closed_lineage_handover(self.root, lone_record), P.LineageHandover(False, frozenset()))
        self._close(lone_child)
        self.assertEqual(P.closed_lineage_handover(self.root, lone_record), P.LineageHandover(True, frozenset()))


class CycleBucketDeclarationTest(unittest.TestCase):
    """CORE §3 "Cycle payload buckets" is the declaration readers such as Cairn
    trust; it must name exactly the buckets the producer types."""

    def test_core_cycle_bucket_table_equals_bucket_types(self):
        core = Path(__file__).resolve().parents[1] / "core" / "CORE.md"
        text = core.read_text(encoding="utf-8")
        start = text.index("**Cycle payload buckets.**")
        rows = {}
        for line in text[start:].split("**Campaign closure.**", 1)[0].splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[0].startswith("`") and cells[2].startswith("`"):
                rows[cells[0].strip("`").rstrip("/")] = cells[2].strip("`")
        self.assertEqual(set(rows), set(P.BUCKET_TYPES))
        self.assertTrue(set(rows.values()) <= {"C-DUR", "C-INT"}, rows)
        # The primary auto-nomination skips exactly the CORE §3 `C-INT` names that
        # are not themselves a cycle bucket (`reviews/` is support only at the root).
        section = text[text.index("## 3. Artifact Root"):text.index("## 3.1.")]
        c_int = set()
        for line in section.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[2] == "`C-INT`":
                c_int |= {name.rstrip("/") for name in cells[0].replace("`", "").replace(",", " ").split()}
        self.assertEqual(P.SUPPORT_SEGMENTS, c_int - set(rows))


class PrimarySupportExclusionTest(unittest.TestCase):
    """2026-09-24 TF-Rehancer: a research cycle sealed `_internal/prompts/report.md`
    as its primary, so Cairn (which does not list C-INT paths) dropped the cycle."""

    def test_support_paths_are_not_auto_nominated_while_output_exists(self):
        rows = [("artifacts/_internal/prompts/report.md", b""),
                ("artifacts/shards/retrieval/survey.md", b""),
                ("artifacts/research/related-work/survey.md", b"")]
        self.assertEqual(P._choose_primary(rows, None), "artifacts/research/related-work/survey.md")
        rows.append(("artifacts/research/final_report.md", b""))
        self.assertEqual(P._choose_primary(rows, None), "artifacts/research/final_report.md")

    def test_audit_report_under_reviews_is_output(self):
        # 2026-09-24 user: audit reports are shown, so `reviews/` is a durable bucket.
        rows = [("artifacts/_internal/audit-brief.md", b""),
                ("artifacts/reviews/audit/audit-report.md", b"")]
        self.assertEqual(P._choose_primary(rows, None), "artifacts/reviews/audit/audit-report.md")

    def test_support_only_cycle_keeps_its_first_row(self):
        rows = [("artifacts/_internal/dispatch/retrieval_prompt.md", b""),
                ("artifacts/shards/retrieval/_internal/raw.txt", b"")]
        self.assertEqual(P._choose_primary(rows, None), "artifacts/_internal/dispatch/retrieval_prompt.md")

    def test_explicit_primary_inside_support_still_wins(self):
        rows = [("artifacts/_internal/notes.md", b""), ("artifacts/plans/report.md", b"")]
        self.assertEqual(P._choose_primary(rows, "_internal/notes.md"), "artifacts/_internal/notes.md")

    def test_official_prd_selection_respects_component_scope_explicit_choice_and_other_capability(self):
        rows = [("artifacts/spec/REPORT.md", b"PASS"), ("artifacts/spec/prd.md", b"root"),
                ("artifacts/spec/a/prd.md", b"a"), ("artifacts/spec/b/prd.md", b"b")]
        whole = {"capability": "autopilot-spec", "nodes": [{"write_scope": ["spec/**"]}]}
        component = {"capability": "autopilot-spec", "nodes": [{"write_scope": ["spec/b/**"]}]}
        self.assertEqual(P._choose_primary(rows, None, route=whole), "artifacts/spec/prd.md")
        self.assertEqual(P._choose_primary(rows, None, route=component), "artifacts/spec/b/prd.md")
        self.assertEqual(P._choose_primary(rows, "spec/a/prd.md", route=component), "artifacts/spec/a/prd.md")
        self.assertEqual(P._choose_primary(rows[:1], None, route=whole), "artifacts/spec/REPORT.md")
        self.assertIsNone(P.official_spec_primary(component, ["artifacts/spec/prd.md", "artifacts/spec/a/prd.md"]))
        # The unchanged generic fallback is case-sensitive: uppercase REPORT.md
        # is not its report.md candidate, so prd.md still wins for this input.
        self.assertEqual(P._choose_primary(rows, None, route={"capability": "autopilot-code"}),
                         "artifacts/spec/prd.md")
        generic_report = [("artifacts/documents/report.md", b"generic report"), *rows]
        self.assertEqual(P._choose_primary(generic_report, None, route={"capability": "autopilot-code"}),
                         "artifacts/documents/report.md")
        self.assertEqual(P._choose_primary(generic_report, None, route=whole), "artifacts/spec/prd.md")


class SharedSpecMergeTest(SharedBaseGuardTest):
    """D-122 integration: immutable inputs, derived publication and exact CAS."""
    def trees(self, *trees):
        self.activate()
        route, route_file, result = self.begin("direct", "autopilot-spec", "update")
        for n, tree in enumerate(trees):
            for path, data in tree.items():
                self.write_output(result, f"gen{n}/{path}", data)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def fixture(self):
        base = {"a/prd.md": b"# A\n\n## One\none\n\n## Two\ntwo\n", "b/prd.md": b"# B\nold\n"}
        first = {**base, "a/prd.md": base["a/prd.md"].replace(b"one\n", b"ONE\n")}
        second = {**base, "a/prd.md": base["a/prd.md"].replace(b"two\n", b"TWO\n")}
        cycle = self.trees(base, first, second, {**second, "b/prd.md": b"# B\nnew\n"})
        initial = self._admit(cycle, 0)
        winner = self._admit(cycle, 1, base_revision=initial["shared_reference_revision_id"])
        return cycle, initial, winner

    def test_disjoint_sections_merge_source_stays_sealed_and_exact_retry_reuses(self):
        cycle, initial, winner = self.fixture()
        source = Path(cycle["cycle_dir"]) / "artifacts/gen2"
        before = P._spec_bytes(source)
        merged = self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        result = P._spec_bytes(Path(merged["revision_dir"]), revision=True)
        self.assertIn(b"ONE\n", result["a/prd.md"])
        self.assertIn(b"TWO\n", result["a/prd.md"])
        self.assertEqual(P._spec_bytes(source), before)
        self.assertEqual(merged["spec_merge"]["latest_revision_id"], winner["shared_reference_revision_id"])
        later = self._admit(cycle, 3, base_revision=initial["shared_reference_revision_id"])
        retry = self._admit(cycle, 2)
        self.assertEqual(retry["status"], "reused")
        self.assertEqual(retry["shared_reference_revision_id"], merged["shared_reference_revision_id"])
        self.assertEqual(self._reference(initial["shared_reference_id"])["latest_revision_id"], later["shared_reference_revision_id"])
        self.assertEqual(self._journals(), [])

    def test_new_latest_component_is_kept_and_old_omission_still_refuses(self):
        base = {"a/prd.md": b"a", "b/prd.md": b"b"}
        cycle = self.trees(base, {**base, "c/prd.md": b"c"}, {**base, "b/prd.md": b"B"}, {"b/prd.md": b"B"})
        initial = self._admit(cycle, 0)
        self._admit(cycle, 1, base_revision=initial["shared_reference_revision_id"])
        merged = self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        self.assertEqual((Path(merged["revision_dir"]) / "c/prd.md").read_bytes(), b"c")
        with self.assertRaises(P.ProducerError) as exc:
            self._admit(cycle, 3, base_revision=initial["shared_reference_revision_id"])
        self.assertEqual(exc.exception.code, "component-set-regressed")
        self.assertEqual(self._journals(), [])

    def test_same_section_conflict_has_no_publication_residue(self):
        base = {"a/prd.md": b"# A\n## One\nold\n"}
        cycle = self.trees(base, {"a/prd.md": b"# A\n## One\nleft\n"}, {"a/prd.md": b"# A\n## One\nright\n"})
        initial = self._admit(cycle, 0)
        self._admit(cycle, 1, base_revision=initial["shared_reference_revision_id"])
        before = self._reference(initial["shared_reference_id"])
        with self.assertRaises(P.ProducerError) as exc:
            self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        self.assertEqual(exc.exception.code, "shared-spec-conflict")
        self.assertIn("One", exc.exception.detail)
        self.assertEqual(self._reference(initial["shared_reference_id"]), before)
        self.assertEqual(self._journals(), [])
        self.assertEqual(ComponentSetPreservation._staging_leftovers(self, initial["shared_reference_id"]), [])

    def test_drop_component_conflicts_with_concurrent_new_file(self):
        base = {"a/prd.md": b"a", "b/prd.md": b"b"}
        cycle = self.trees(base, {**base, "a/new.md": b"new"}, {"b/prd.md": b"b"})
        initial = self._admit(cycle, 0)
        self._admit(cycle, 1, base_revision=initial["shared_reference_revision_id"])
        with self.assertRaises(P.ProducerError) as exc:
            self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"], drop_components=["a"])
        self.assertEqual(exc.exception.code, "shared-spec-conflict")
        self.assertEqual(self._journals(), [])

    def test_base_and_latest_bytes_are_verified_before_admission(self):
        cycle, initial, winner = self.fixture()
        for record in (initial, winner):
            path = Path(record["revision_dir"]) / "b/prd.md"
            old = path.read_bytes()
            path.write_bytes(b"tampered")
            try:
                with self.assertRaises(P.ProducerError) as exc:
                    self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
                self.assertEqual(exc.exception.code, "shared-revision-integrity")
                self.assertEqual(self._journals(), [])
            finally:
                path.write_bytes(old)

    def test_publish_interruption_recovers_exact_proof_and_rejects_corruption(self):
        from unittest import mock
        cycle, initial, winner = self.fixture()
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        journal_path = next(P.shared_journal_path(self.root, "probe").parent.glob("*.json"))
        journal = json.loads(journal_path.read_text())
        output = self.root / journal["target"] / "a/prd.md"
        original = output.read_bytes()
        output.write_bytes(b"corruption")
        self.assertTrue(P._recover_locked(self.root)["unresolved"])
        self.assertEqual(self._reference(initial["shared_reference_id"])["latest_revision_id"], winner["shared_reference_revision_id"])
        output.write_bytes(original)
        recovered = P._recover_locked(self.root)
        self.assertEqual(recovered["unresolved"], [])
        self.assertFalse(journal_path.exists())
        self.assertEqual(self._admit(cycle, 2)["shared_reference_revision_id"], journal["revision_id"])

    def test_retry_validates_proof_and_published_bytes(self):
        cycle, initial, _ = self.fixture()
        merged = self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        record_path = Path(merged["revision_dir"]) / P.REVISION_RECORD_NAME
        record = json.loads(record_path.read_text())
        record["spec_merge"]["source_content_digest"] = "sha256:" + "0" * 64
        record_path.write_text(json.dumps(record))
        with self.assertRaises(P.ProducerError) as exc:
            self._admit(cycle, 2)
        self.assertEqual(exc.exception.code, "shared-merge-proof-invalid")

    def test_exact_base_and_exact_retry_reject_corrupt_base(self):
        cycle, initial, winner = self.fixture()
        for base, operation in ((winner, lambda: self._admit(cycle, 2, base_revision=winner["shared_reference_revision_id"])),
                                (initial, lambda: self._admit(cycle, 1))):
            path = Path(base["revision_dir"]) / "b/prd.md"
            data = path.read_bytes(); path.write_bytes(b"tampered base")
            try:
                with self.assertRaises(P.ProducerError) as exc: operation()
                self.assertEqual(exc.exception.code, "shared-revision-integrity")
                self.assertEqual(self._journals(), [])
            finally:
                path.write_bytes(data)

    def test_missing_or_malformed_canonical_base_is_never_legacy(self):
        cycle, initial, winner = self.fixture()
        for base, operation in ((winner, lambda: self._admit(cycle, 2, base_revision=winner["shared_reference_revision_id"])),
                                (initial, lambda: self._admit(cycle, 1))):
            path = Path(base["revision_dir"]) / P.REVISION_RECORD_NAME
            original = path.read_bytes()
            for bad in (None, b"not-json"):
                if bad is None: path.unlink()
                else: path.write_bytes(bad)
                try:
                    with self.assertRaises(P.ProducerError) as exc: operation()
                    self.assertEqual(exc.exception.code, "shared-base-invalid")
                finally:
                    path.write_bytes(original)

    def test_removed_exact_base_field_still_verifies_parent_bytes(self):
        cycle, initial, winner = self.fixture()
        record_path = Path(winner["revision_dir"]) / P.REVISION_RECORD_NAME
        record = json.loads(record_path.read_text()); record.pop("spec_base_revision_id")
        record_path.write_text(json.dumps(record))
        (Path(initial["revision_dir"]) / "b/prd.md").write_bytes(b"tampered parent")
        with self.assertRaises(P.ProducerError) as exc: self._admit(cycle, 1)
        self.assertEqual(exc.exception.code, "shared-revision-integrity")

    def test_unresolved_publication_blocks_duplicate_attempt(self):
        from unittest import mock
        cycle, initial, winner = self.fixture()
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        journal_path = next(P.shared_journal_path(self.root, "probe").parent.glob("*.json"))
        journal = json.loads(journal_path.read_text())
        (self.root / journal["target"] / "a/prd.md").write_bytes(b"corruption")
        before = sorted(p.name for p in (self.root / journal["target"]).parent.iterdir())
        with self.assertRaises(P.ProducerError) as exc:
            self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        self.assertEqual(exc.exception.code, "shared-publication-unresolved")
        self.assertEqual(sorted(p.name for p in (self.root / journal["target"]).parent.iterdir()), before)
        self.assertTrue(journal_path.exists())

    def test_drop_metadata_is_bound_to_crash_recovery(self):
        from unittest import mock
        base = {"a/prd.md": b"a", "b/prd.md": b"b"}
        cycle = self.trees(base, {**base, "b/prd.md": b"B"}, {"b/prd.md": b"b"})
        initial = self._admit(cycle, 0)
        self._admit(cycle, 1, base_revision=initial["shared_reference_revision_id"])
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"],
                            drop_components=["a"], drop_reason="retired")
        journal_path = next(P.shared_journal_path(self.root, "probe").parent.glob("*.json"))
        journal = json.loads(journal_path.read_text())
        record_path = self.root / journal["target"] / P.REVISION_RECORD_NAME
        original = record_path.read_bytes(); record = json.loads(original)
        record.pop("dropped_components"); record_path.write_text(json.dumps(record))
        self.assertTrue(P._recover_locked(self.root)["unresolved"])
        record_path.write_bytes(original)
        self.assertEqual(P._recover_locked(self.root)["unresolved"], [])

    def test_malformed_and_unsafe_journals_are_preserved_without_deletion(self):
        cycle, initial, winner = self.fixture()
        directory = P.shared_journal_path(self.root, "probe").parent
        directory.mkdir(parents=True, exist_ok=True)
        bad = directory / ("rrev_" + "e" * 32 + ".json")
        sentinel = self.root / "keep.txt"; sentinel.write_text("keep")
        for payload in (b"not json", P._json_bytes({"kind": "spec", "revision_id": bad.stem,
                "reference_id": initial["shared_reference_id"], "state": "staging", "staging": "",
                "target": "", "expected_previous_revision_id": winner["shared_reference_revision_id"]})):
            bad.write_bytes(payload)
            result = P._recover_locked(self.root)
            self.assertTrue(result["unresolved"])
            self.assertEqual(bad.read_bytes(), payload)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_stripped_merge_proof_cannot_be_recovered_as_exact_copy(self):
        from unittest import mock
        cycle, initial, winner = self.fixture()
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._admit(cycle, 2, base_revision=initial["shared_reference_revision_id"])
        journal_path = next(P.shared_journal_path(self.root, "probe").parent.glob("*.json"))
        journal = json.loads(journal_path.read_text())
        record_path = self.root / journal["target"] / P.REVISION_RECORD_NAME
        record = json.loads(record_path.read_text()); record.pop("spec_merge")
        record_path.write_text(json.dumps(record)); journal.pop("spec_merge")
        journal_path.write_text(json.dumps(journal))
        self.assertEqual(P._recover_locked(self.root)["unresolved"][0]["code"], "shared-merge-proof-invalid")

    def test_parallel_publishers_preserve_both_deltas(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        base = {"a/prd.md": b"a", "b/prd.md": b"b"}
        cycle = self.trees(base, {**base, "a/prd.md": b"A"}, {**base, "b/prd.md": b"B"})
        initial = self._admit(cycle, 0)
        queue = ctx.Queue()
        def publish(n):
            try:
                row = self._admit(cycle, n, base_revision=initial["shared_reference_revision_id"])
                queue.put(("ok", row["shared_reference_revision_id"]))
            except Exception as exc:
                queue.put(("error", str(exc)))
        children = [ctx.Process(target=publish, args=(n,)) for n in (1, 2)]
        try:
            for child in children: child.start()
            for child in children:
                child.join(20)
                self.assertFalse(child.is_alive(), "publisher failed bounded join")
                self.assertEqual(child.exitcode, 0)
            rows = [queue.get(timeout=3) for _ in children]
            self.assertTrue(all(r[0] == "ok" for r in rows), rows)
            ref = self._reference(initial["shared_reference_id"])
            tree, _ = P._verified_shared_spec(self.root, initial["shared_reference_id"], ref["latest_revision_id"])
            self.assertEqual(tree, {"a/prd.md": b"A", "b/prd.md": b"B"})
            self.assertEqual(len(ref["revisions"]), 3)
        finally:
            for child in children:
                if child.is_alive(): child.terminate()
                child.join(3)
            queue.close()
            queue.join_thread()


class SealBackgroundJobTest(ProducerTestBase):
    """Sealing starts exactly one background judgement (the unified review); no title job exists any more,
    and no failure of the trigger changes the seal."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("HEARTING_WORKFLOW_GROUP_REVIEW", "HEARTING_CAMPAIGN_TITLE_AUTO"):
            os.environ.pop(name, None)  # the runner switches both background jobs off

    def _seal(self, slug="seal-job"):
        route, route_file = self.route(slug=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(result)
        self.close(route, route_file)
        return result

    def test_a_seal_launches_the_review_once_and_never_a_title_job(self):
        import artifact_workflow_group_review as review
        import campaign_title_repair as title_repair
        self.activate()
        result = self._seal()
        with mock.patch.object(review, "launch_after_seal", return_value=True) as launched, \
                mock.patch.object(title_repair, "launch_after_seal", return_value=True) as titled, \
                mock.patch.object(title_repair, "auto_title") as auto_title:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(launched.call_count, 1)
        self.assertEqual(launched.call_args.args[1]["cycle_id"], result["cycle_id"])
        self.assertEqual(launched.call_args.args[1]["state"], "sealed")
        titled.assert_not_called()
        auto_title.assert_not_called()
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "sealed")

    def test_a_failing_or_spawn_failing_trigger_never_changes_the_seal(self):
        import artifact_workflow_group_review as review
        self.activate()
        first = self._seal("first")
        with mock.patch.object(review, "launch_after_seal", side_effect=RuntimeError("boom")):
            self.assertEqual(P.finalize(self.root, cycle_id=first["cycle_id"])["status"], "sealed")
        second = self._seal("second")
        with mock.patch.object(review, "in_test_process", return_value=False), \
                mock.patch.object(review.subprocess, "Popen", side_effect=OSError("no fork")) as popen:
            self.assertEqual(P.finalize(self.root, cycle_id=second["cycle_id"])["status"], "sealed")
        self.assertEqual(popen.call_count, 1)  # one launch attempt, from the review alone
        self.assertIn("artifact_workflow_group_review.py", popen.call_args.args[0][1])
        self.assertEqual(P.read_cycle_record(self.root, second["cycle_id"])["state"], "sealed")

    def test_the_title_switch_alone_does_not_stop_the_review_launch(self):
        import artifact_workflow_group_review as review
        self.activate()
        result = self._seal()
        with mock.patch.dict(os.environ, {"HEARTING_CAMPAIGN_TITLE_AUTO": "off"}), \
                mock.patch.object(review, "in_test_process", return_value=False), \
                mock.patch.object(review.subprocess, "Popen") as popen:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        popen.assert_called_once()


class QuickPreviewApprovalWriteGuardTest(ProducerTestBase):
    def test_bound_complete_scope_allows_quick_refine_target_write(self):
        target = self.root / "documents" / "sample" / "draft.md"
        target.parent.mkdir(parents=True)
        route = {"capability": "autopilot-refine", "effective_intensity": "quick",
                 "route_plan": {"decision": "decision.json", "digest": "sha256:" + "a" * 64, "index": 0},
                 "entry_execution_scope": "complete", "entry_scope_contract_version": 1,
                 "nodes": [{"id": "one-shot"}]}
        P._quick_refine_write_gate(self.root, target, route)

    def test_direct_report_scope_blocks_source_write_but_allows_its_preview_output(self):
        target = self.root / "documents" / "sample" / "draft.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"original artifact bytes\n")
        original = target.read_bytes()
        report = {"capability": "autopilot-refine", "effective_intensity": "direct",
                  "entry_scope_contract_version": 1, "entry_execution_scope": "report",
                  "nodes": [{"id": "one-shot"}]}
        with self.assertRaisesRegex(P.ProducerError, "legacy-top-level-write-denied"):
            P._quick_refine_write_gate(self.root, target, report)
        snapshot = self.root / "documents" / "sample" / "_internal" / "versions" / "1" / "draft.md"
        with self.assertRaisesRegex(P.ProducerError, "legacy-top-level-write-denied"):
            P._quick_refine_write_gate(self.root, snapshot, report)
        history = self.root / "documents" / "sample" / "pipeline_summary.md"
        with self.assertRaisesRegex(P.ProducerError, "legacy-top-level-write-denied"):
            P._quick_refine_write_gate(self.root, history, report)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(snapshot.exists())
        self.assertFalse(history.exists())
        preview = self.root / "reviews" / "refine" / "preview.md"
        P._quick_refine_write_gate(self.root, preview, report)

    def test_report_and_unreleased_old_route_remain_protected(self):
        target = self.root / "documents" / "sample" / "draft.md"
        target.parent.mkdir(parents=True)
        binding = {"decision": "decision.json", "digest": "sha256:" + "a" * 64, "index": 0}
        report = {"capability": "autopilot-refine", "effective_intensity": "quick",
                  "route_plan": binding, "entry_execution_scope": "report", "entry_scope_contract_version": 1,
                  "nodes": [{"id": "one-shot"}]}
        with self.assertRaisesRegex(P.ProducerError, "legacy-top-level-write-denied"):
            P._quick_refine_write_gate(self.root, target, report)
        old = {"capability": "autopilot-refine", "effective_intensity": "quick",
               "nodes": [{"id": "one-shot"}]}
        with self.assertRaisesRegex(P.ProducerError, "inline-gate-binding-missing"):
            P._quick_refine_write_gate(self.root, target, old)


if __name__ == "__main__":
    unittest.main()
