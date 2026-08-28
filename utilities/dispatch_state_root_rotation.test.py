#!/usr/bin/env python3
"""I-2 regression (assignment 검증요구 (a) / plan-check round-1 T4, frame §4):
a completion marker written under the canonical dispatch state root
(dirname of AGENT_DISPATCH_JOBS) must survive a release rotation that
physically deletes the old packaged AGENT_HOME (tools/install/distribution.py
_cleanup_releases), and every reader -- completion_marker_gate,
dispatch-registry.py, tools/fleet/route.py -- must still find the same
marker afterward. Before this cycle, the writer used
$AGENT_HOME/.dispatch/completion, which _cleanup_releases deletes wholesale
every second rotation.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utilities"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROUTE = _load("route_for_rotation_test", "utilities/capability-route.py")
REGISTRY = _load("dispatch_registry_for_rotation_test", "utilities/dispatch-registry.py")
FLEET_ROUTE = _load("fleet_route_for_rotation_test", "tools/fleet/route.py")
DISTRIBUTION = _load("distribution_for_rotation_test", "tools/install/distribution.py")
import dispatch_contract as DC  # noqa: E402


class DispatchStateRootRotationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

        # Fake runtime-state home: a stable location that survives rotation.
        self.runtime_jobs = self.base / "rt" / ".harness" / "dispatch" / "jobs.log"

        # Fake packaged release: this whole tree gets rmtree'd, simulating
        # tools/install/distribution.py _cleanup_releases dropping an old
        # release once a newer one has been kept.
        self.release = self.base / "releases" / "v1"
        (self.release / "core").mkdir(parents=True)
        (self.release / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")

        self.repo = self.base / "repo"
        self.repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / "x").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)

        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()

        self._prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "CLAUDE_HOME")
        }
        os.environ["AGENT_HOME"] = str(self.release)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.runtime_jobs)
        os.environ.pop("CLAUDE_HOME", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _compile_route(self):
        rows = [{
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "rotation-fixture", "probe_time": "2026-08-12T00:00:00Z",
            "failure_class": "", "checked_worktree": str(self.repo.resolve()),
            "failure_scope": "none", "codex_command": "ok",
            "retry_on_isolated_worktree": 0,
        }]
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        return ROUTE.compile_route(
            "autopilot-code", "dev", "strong", self.repo, self.artifact,
            signals=["shared-contract"], transport="headless", tracking="tracked",
            tracked_gate_evidence=gate,
            dispatch_evidence={"tuples": rows, "native_subagent": []},
        )

    def test_marker_survives_release_rotation_across_all_readers(self):
        route = self._compile_route()
        node = next(n for n in route["nodes"] if n["id"] == "plan")
        evidence = self.base / "plan.md"
        evidence.write_text("plan\n", encoding="utf-8")

        # complete_node() (not write_completion_marker() directly) is the real
        # entry point every wrapper uses -- it also publishes the exact-attempt
        # linkage sidecar that completion_marker_is_current()/the gate require.
        marker, _ = ROUTE.complete_node(
            route, node, "plan", evidence,
            attempt_id="att-rotation-fixture",
            explicit_attempt_metadata={
                "attempt_schema_version": 2,
                "dispatch_depth": 2,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
            },
        )

        # State root is beside the runtime registry, never inside the release.
        state_root = DC.dispatch_state_root(self.runtime_jobs)
        canonical_path = state_root / "completion" / route["route_id"] / "plan.json"
        self.assertTrue(canonical_path.is_file())
        self.assertFalse(str(canonical_path).startswith(str(self.release)))

        # Simulate _cleanup_releases dropping the packaged root entirely.
        shutil.rmtree(self.release)
        self.assertFalse(self.release.exists())

        # Reader 1: dispatch_contract.completion_marker_gate (called with a
        # fabricated depends_on to force the marker lookup path).
        route_with_dep = dict(route)
        route_with_dep["nodes"] = [
            dict(n, depends_on=["plan"]) if n["id"] == "execute" else n
            for n in route["nodes"]
        ]
        route_file = self.base / "route.json"
        route_file.write_text(json.dumps(route_with_dep), encoding="utf-8")
        # completion_marker_gate must not raise completion-marker-missing for
        # the now-satisfied "plan" dependency of "execute".
        try:
            DC.completion_marker_gate(
                str(route_file), "execute", "start",
                self.release, self.runtime_jobs,
            )
        except DC.DispatchContractError as exc:
            self.assertNotEqual(
                exc.reason, "completion-marker-missing",
                f"reader 1 (completion_marker_gate) lost the marker after rotation: {exc.detail}",
            )

        # Reader 2: dispatch-registry.py's route_incomplete() -- the same
        # primitive Fleet's orphan classifier and preflight status use.
        fake_row = {
            "repo": str(self.repo), "worktree": str(self.repo), "slug": "owner",
            "meta": {
                "route_id": route["route_id"], "route_file": str(route_file),
                "attempt_id": "att-owner-fixture",
            },
        }
        incomplete, status = REGISTRY.route_incomplete(
            fake_row, self.release, rows=[fake_row], jobs=self.runtime_jobs,
        )
        self.assertEqual(status, "ok")
        self.assertNotIn("plan", incomplete)

        # Reader 3: tools/fleet/route.py gate_mark() -- the Fleet UI's own
        # completion-marker reader.
        passed = FLEET_ROUTE.gate_mark(route, "plan", home=str(self.release))
        self.assertIs(passed, True)

        # Read-fallback: an explicit legacy-relative candidate (no state root
        # override) must not find anything new post-rotation -- the writer
        # never wrote there -- proving the marker really lived at the new
        # root, not merely readable through a coincidence.
        legacy_marker = self.release / ".dispatch" / "completion" / route["route_id"] / "plan.json"
        self.assertFalse(legacy_marker.exists())


class IdempotentRepublishAcrossSpellingTest(unittest.TestCase):
    """Review P-1 (round-3 blocking): a republish of the same attempt from a
    process whose AGENT_HOME spells the same directory differently (pointer
    symlink vs resolved release) must be an idempotent no-op. Before the fix,
    _publish_completion_locked rebuilt the attempt sidecar in the current
    spelling and write_once's whole-byte comparison raised on every retry --
    the canonical recovery path (idempotent republish) was itself broken."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.release = self.base / "releases" / "v1"
        (self.release / "core").mkdir(parents=True)
        (self.release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        self.current = self.base / "current"
        self.current.symlink_to(self.release)
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text("evidence\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-p1",
            "route_hash": "h" * 8,
            "registry_digest": "d" * 8,
        }
        self.node = {
            "completion_gate": "artifact",
            "dispatch_depth": 0,
            "execution_surface": "inline",
            "kind": "stage",
        }
        # SD-112 §13.33.2-(8) chain-3 supersession: the env-less fallback no
        # longer resolves relative to AGENT_HOME at all (pointer vs resolved
        # spelling is no longer part of its path derivation), so this
        # fixture must pin an isolated stable root instead of asserting
        # AGENT_HOME's own spelling ends up embedded in it -- and must not
        # touch the real developer HOME (C3) doing so.
        self.stable_home = self.base / "stable-home"
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ["HOME"] = str(self.stable_home)
        self.stable_completion = (
            self.stable_home / ".local" / "state" / "hearting" / "dispatch"
            / "completion" / "rt-p1"
        )

    def tearDown(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _publish(self, home):
        os.environ["AGENT_HOME"] = str(home)
        return ROUTE._publish_completion_locked(
            self.route, self.node, "plan", self.evidence,
            attempt_id="att-p1", attempt_metadata=None,
        )

    def test_republish_with_resolved_spelling_is_idempotent(self):
        self._publish(self.current)
        sidecars = list(
            self.stable_completion.glob("plan.att-*.attempt.json")
        )
        self.assertEqual(len(sidecars), 1)
        original_bytes = sidecars[0].read_bytes()
        recorded = json.loads(original_bytes)
        # Chain-3 no longer varies by AGENT_HOME spelling at all -- the
        # marker always lands under the one stable root.
        self.assertIn(str(self.stable_home), recorded["completion_marker"])

        # Same attempt, same files, AGENT_HOME now spelled as the resolved
        # release: must not raise, and must not rewrite the origin bytes.
        self._publish(self.release)
        self.assertEqual(sidecars[0].read_bytes(), original_bytes)


class FleetGateMarkSpellingFlipTest(unittest.TestCase):
    """Round-5 S-5 (non-blocking, regression added anyway): T-4 changed
    `tools/fleet/route.py`'s `gate_mark` self-ref comparison from a verbatim
    string check to `Path(...).resolve(strict=False)` identity, matching
    `agent_home_equivalent`. The fix itself is correct (owner/anchor both
    confirmed True/None -> True/True), but round-5 found no regression pins
    it -- reverting T-4 alone still left the existing rotation suite 3/3
    green, because that suite always calls `gate_mark` with the SAME home
    spelling the marker was written under. This test publishes under a
    symlinked (pointer-form) AGENT_HOME and reads with `home=` spelled as
    the resolved release, exercising the exact spelling-flip grid T-4
    fixed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.release = self.base / "releases" / "v1"
        (self.release / "core").mkdir(parents=True)
        (self.release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        self.current = self.base / "current"
        self.current.symlink_to(self.release)
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text("evidence\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-gm1",
            "route_hash": "h" * 8,
            "registry_digest": "d" * 8,
            "schema_version": 2,
            "dispatch_contract_version": 3,
            "nodes": [{
                "id": "plan",
                "completion_gate": "artifact",
                "dispatch_depth": 0,
                "execution_surface": "inline",
                "kind": "stage",
            }],
        }
        self.node = self.route["nodes"][0]
        # SD-112 §13.33.2-(8) chain-3 supersession (see
        # IdempotentRepublishAcrossSpellingTest): the env-less fallback no
        # longer varies by AGENT_HOME spelling, so pin an isolated stable
        # root instead of the release tree, and never touch the real
        # developer HOME (C3) doing so.
        self.stable_home = self.base / "stable-home"
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ["HOME"] = str(self.stable_home)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_gate_mark_true_across_pointer_and_resolved_spelling(self):
        os.environ["AGENT_HOME"] = str(self.current)
        ROUTE._publish_completion_locked(
            self.route, self.node, "plan", self.evidence,
            attempt_id="att-gm1", attempt_metadata=None,
        )
        sidecar = (
            self.stable_home / ".local" / "state" / "hearting" / "dispatch"
            / "completion" / "rt-gm1" / "plan.att-gm1.attempt.json"
        )
        self.assertTrue(sidecar.is_file())
        recorded = json.loads(sidecar.read_text())
        # Chain-3 no longer varies by AGENT_HOME spelling -- the marker
        # always lands under the one stable root, regardless of it.
        self.assertIn(str(self.stable_home), recorded["completion_marker"])

        # gate_mark is called with `home` spelled as the resolved release,
        # NOT the pointer form the marker was written under -- the
        # self-ref comparison inside gate_mark must resolve both sides,
        # not compare verbatim strings (that is what T-4 fixed).
        passed = FLEET_ROUTE.gate_mark(self.route, "plan", home=str(self.release))
        self.assertIs(passed, True)


class ReanchorImplementationParityTest(unittest.TestCase):
    """Round-5 S-6 (non-blocking, regression added anyway): two independent
    re-anchor implementations exist -- `utilities/capability-route.py`'s
    `_rewrite_migrated_attempt_links` (legacy-tree migration) and
    `tools/install/distribution.py`'s `_reanchor_succeeded_attempt_links`
    (release-rotation succession) -- because `tools/install/` intentionally
    does not import `utilities/` (T-1's boundary judgment, not reversed
    here). Round-5 found their serialization byte-identical but nothing
    enforcing that stays true (round-2 N-5 was exactly this drift). This
    pins byte-for-byte output parity across the shared, non-adversarial
    input domain both helpers actually see in production: a self-ref value
    nested under the old directory (ordinary re-anchor), a value carrying
    non-ASCII bytes, an unrelated key that must stay untouched, and a
    malformed sidecar that must be left byte-identical to its input. It does
    NOT assert parity on the round-5 S-1 prefix-collision grid --
    `_reanchor_succeeded_attempt_links` was deliberately hardened past
    `_rewrite_migrated_attempt_links` there because its call site's release
    names are attacker/operator-chosen strings, unlike the fixed-length
    `rt-<16hex>` route ids `_rewrite_migrated_attempt_links` only ever
    receives (review round-5 S-1 "참고" note) -- so intentional divergence
    on that one grid is correct, not drift."""

    def _run(self, fn, label, files, old, new):
        directory = old.parent / f"reanchor-run-{label}"
        directory.mkdir(parents=True)
        for name, payload in files.items():
            (directory / name).write_text(payload, encoding="utf-8")
        fn(directory, old, new)
        return {name: (directory / name).read_bytes() for name in files}

    def test_byte_identical_output_across_shared_grid(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        old = base / "old-dir"
        new = base / "new-dir"
        old.mkdir()

        ordinary = json.dumps({
            "schema_version": 2,
            "completion_marker": str(old / "plan.json"),
            "completion_marker_history": str(old / "plan.1.json"),
        }, indent=2, ensure_ascii=False) + "\n"
        unicode_value = json.dumps({
            "schema_version": 2,
            "completion_marker": str(old / "plan.json"),
            "completion_marker_history": str(old / "plan.1.json"),
            "evidence_path": "/tmp/이현대 evidence 日本語.md",
        }, indent=2, ensure_ascii=False) + "\n"
        unchanged_key = json.dumps({
            "schema_version": 2,
            "completion_marker": "/somewhere/else/plan.json",
            "completion_marker_history": "/somewhere/else/plan.1.json",
        }, indent=2, ensure_ascii=False) + "\n"
        malformed = "{not valid json"

        files = {
            "plan.att-ordinary.attempt.json": ordinary,
            "plan.att-unicode.attempt.json": unicode_value,
            "plan.att-unchanged.attempt.json": unchanged_key,
            "plan.att-malformed.attempt.json": malformed,
        }

        old_impl_output = self._run(
            lambda d, o, n: ROUTE._rewrite_migrated_attempt_links(d, o, n),
            "old", files, old, new,
        )
        new_impl_output = self._run(
            lambda d, o, n: DISTRIBUTION._reanchor_succeeded_attempt_links(d, o, n),
            "new", files, old, new,
        )

        self.assertEqual(set(old_impl_output), set(new_impl_output))
        for name in files:
            self.assertEqual(
                old_impl_output[name], new_impl_output[name],
                f"re-anchor output byte-diverged for {name}",
            )
        # Both must have actually rewritten the ordinary/unicode cases (a
        # trivially-passing no-op parity would not be evidence of anything).
        self.assertIn(str(new), old_impl_output["plan.att-ordinary.attempt.json"].decode())
        self.assertIn(str(new), new_impl_output["plan.att-ordinary.attempt.json"].decode())


class IdempotentRepublishAcrossRotationSuccessionTest(unittest.TestCase):
    """Review Q-1 (round-4 blocking): the spelling-only case above (P-1) does
    not cover the case the two-root fallback in
    utilities/capability-route.py:1494-1502 actually exists for -- a release
    rotation that physically deletes the directory the sidecar's self-ref
    recorded, with tools/install/distribution.py's `_succeed_dispatch_state`
    carrying the `.dispatch` tree forward into the new live release first.
    Before the fix, that carry-forward copied the sidecar byte-for-byte
    without re-anchoring its self-referential paths, so a same-attempt
    republish from the new release raised "immutable route already exists
    with different content" -- a regression this cycle's own succession
    behavior introduced (a pristine prune-without-succession republish
    succeeds)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)

        self.release_a = DISTRIBUTION.data_root() / "releases" / "vA"
        self.release_b = DISTRIBUTION.data_root() / "releases" / "vB"
        for release in (self.release_a, self.release_b):
            (release / "core").mkdir(parents=True)
            (release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text("evidence\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-succ",
            "route_hash": "h" * 8,
            "registry_digest": "d" * 8,
        }
        self.node = {
            "completion_gate": "artifact",
            "dispatch_depth": 0,
            "execution_surface": "inline",
            "kind": "stage",
        }

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _publish(self, home):
        os.environ["AGENT_HOME"] = str(home)
        # SD-112 chain-3 supersession: the env-less fallback no longer
        # resolves under a specific release at all, so this fixture -- which
        # exercises _succeed_dispatch_state's release-embedded carry-forward
        # -- must pin the registry explicitly to keep landing there.
        os.environ["AGENT_DISPATCH_JOBS"] = str(Path(home) / ".dispatch" / "jobs.log")
        return ROUTE._publish_completion_locked(
            self.route, self.node, "plan", self.evidence,
            attempt_id="att-succ", attempt_metadata=None,
        )

    def test_republish_after_rotation_succession_is_idempotent(self):
        # 1. chain-(3) session publishes while `current` -> release A.
        self._publish(self.release_a)
        sidecar = (
            self.release_a / ".dispatch" / "completion" / "rt-succ"
            / "plan.att-succ.attempt.json"
        )
        self.assertTrue(sidecar.is_file())
        recorded_before = json.loads(sidecar.read_text())
        self.assertIn(str(self.release_a), recorded_before["completion_marker"])

        # 2. rotation retargets `current` -> release B; _succeed_dispatch_state
        # carries release A's `.dispatch` tree forward before A is deleted.
        current = DISTRIBUTION.current_path()
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(self.release_b)
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.release_a))

        migrated_sidecar = (
            self.release_b / ".dispatch" / "completion" / "rt-succ"
            / "plan.att-succ.attempt.json"
        )
        self.assertTrue(migrated_sidecar.is_file())
        migrated = json.loads(migrated_sidecar.read_text())
        # The carried-forward sidecar must record its NEW location, not the
        # stale release-A path it was copied from -- that is exactly what
        # write_once compares against on republish.
        self.assertIn(str(self.release_b), migrated["completion_marker"])
        self.assertNotIn(str(self.release_a), migrated["completion_marker"])

        shutil.rmtree(self.release_a)

        # 3. same attempt republishes from a chain-(3) session on release B
        # (e.g. a retry after a lost receipt) -- must be a no-op, not a hard
        # failure.
        try:
            self._publish(self.release_b)
        except ValueError as exc:
            self.fail(f"republish after rotation succession raised: {exc}")
        self.assertEqual(migrated_sidecar.read_text(), json.dumps(
            migrated, indent=2, ensure_ascii=False,
        ) + "\n")


class RetryAfterFailedSuccessionStaysFailedTest(unittest.TestCase):
    """Review V-1 (round-6 codex blocking): the first `_succeed_dispatch_state`
    pass correctly returns False on a malformed sidecar and the caller retains
    the candidate. But a retry pass copies nothing (every destination already
    exists), so before the fix the re-anchor set -- derived only from the copy
    set -- was empty, the retry returned True, and `_cleanup_releases` could
    delete the candidate while the live copy was still malformed. A retry must
    stay False until the live copy is actually repaired, and must succeed (with
    a re-anchored live sidecar) once it is."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)
        self.release_a = DISTRIBUTION.data_root() / "releases" / "vA"
        self.release_b = DISTRIBUTION.data_root() / "releases" / "vB"
        for release in (self.release_a, self.release_b):
            (release / "core").mkdir(parents=True)
            (release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_retry_with_malformed_live_sidecar_stays_failed(self):
        stale_dir = self.release_a / ".dispatch" / "completion" / "rt-v1"
        stale_dir.mkdir(parents=True)
        (stale_dir / "plan.att-bad.attempt.json").write_text(
            "{not json", encoding="utf-8"
        )
        current = DISTRIBUTION.current_path()
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(self.release_b)

        # First pass: copy succeeds, re-anchor fails on the malformed JSON.
        self.assertFalse(DISTRIBUTION._succeed_dispatch_state(self.release_a))
        live = (
            self.release_b / ".dispatch" / "completion" / "rt-v1"
            / "plan.att-bad.attempt.json"
        )
        self.assertTrue(live.is_file())

        # Retry: nothing new to copy -- must STILL be False (V-1), so the
        # caller keeps the candidate instead of deleting it.
        self.assertFalse(DISTRIBUTION._succeed_dispatch_state(self.release_a))

        # Repair the live copy with a valid sidecar still recording the old
        # release; the next retry must now succeed AND re-anchor it.
        live.write_text(
            json.dumps(
                {
                    "completion_marker": str(stale_dir / "plan.json"),
                    "completion_marker_history": str(stale_dir / "plan.1.json"),
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.release_a))
        repaired = json.loads(live.read_text(encoding="utf-8"))
        self.assertIn(str(self.release_b), repaired["completion_marker"])
        self.assertNotIn(str(self.release_a), repaired["completion_marker"])


class PrefixCollisionRotationSuccessionTest(unittest.TestCase):
    """Round-5 S-1 (blocking): `IdempotentRepublishAcrossRotationSuccessionTest`
    above uses release names `vA`/`vB`, which are never string prefixes of
    each other, so it cannot see this grid. A plain tag progression
    (`v0.9` -> `v0.9.1`) makes the pruned candidate's name a literal string
    prefix of the live release's name. Before the fix,
    `_reanchor_succeeded_attempt_links`'s unbounded `startswith` rewrote a
    sidecar the *live* release published for itself (not anything carried
    forward from the candidate) into a nonexistent `v0.9.1.1` path, and
    `_succeed_dispatch_state` still returned True so the candidate -- the
    only place the correct sidecar bytes existed -- was deleted."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)

        self.release_a = DISTRIBUTION.data_root() / "releases" / "v0.9"
        self.release_b = DISTRIBUTION.data_root() / "releases" / "v0.9.1"
        for release in (self.release_a, self.release_b):
            (release / "core").mkdir(parents=True)
            (release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text("evidence\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-pfx",
            "route_hash": "h" * 8,
            "registry_digest": "d" * 8,
        }
        self.node = {
            "completion_gate": "artifact",
            "dispatch_depth": 0,
            "execution_surface": "inline",
            "kind": "stage",
        }

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _publish(self, home, node_id):
        os.environ["AGENT_HOME"] = str(home)
        # SD-112 chain-3 supersession: see IdempotentRepublishAcrossRotation
        # SuccessionTest -- pin the registry explicitly under `home`.
        os.environ["AGENT_DISPATCH_JOBS"] = str(Path(home) / ".dispatch" / "jobs.log")
        return ROUTE._publish_completion_locked(
            self.route, self.node, node_id, self.evidence,
            attempt_id=f"att-{node_id}", attempt_metadata=None,
        )

    def test_live_sidecar_survives_rotation_succession_with_prefix_collision(self):
        # 1. chain-(3) session publishes route rt-pfx's "plan" node while
        # `current` -> v0.9 (about to be pruned).
        self._publish(self.release_a, "plan")

        # 2. rotation retargets `current` -> v0.9.1 (the live release, whose
        # name is a string extension of v0.9's).
        current = DISTRIBUTION.current_path()
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(self.release_b)

        # 3. the live release publishes rt-pfx's "execute" node under its own
        # power -- this sidecar was never copied from v0.9 and must not be
        # touched by v0.9's carry-forward.
        self._publish(self.release_b, "execute")
        live_sidecar = (
            self.release_b / ".dispatch" / "completion" / "rt-pfx"
            / "execute.att-execute.attempt.json"
        )
        self.assertTrue(live_sidecar.is_file())
        live_bytes_before = live_sidecar.read_bytes()

        # 4. prune v0.9; its .dispatch tree carries forward into v0.9.1.
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.release_a))

        # The live sidecar's bytes -- including its self-referential path --
        # must be byte-for-byte unchanged. A boundary-less prefix match
        # rewrites "v0.9.1/..." into the nonexistent "v0.9.1.1/...".
        self.assertTrue(live_sidecar.is_file())
        self.assertEqual(live_sidecar.read_bytes(), live_bytes_before)
        corrupted = (
            DISTRIBUTION.data_root() / "releases" / "v0.9.1.1"
        )
        self.assertFalse(corrupted.exists())

        # The carried-forward "plan" sidecar must still be re-anchored to
        # its new (real) location.
        migrated_sidecar = (
            self.release_b / ".dispatch" / "completion" / "rt-pfx"
            / "plan.att-plan.attempt.json"
        )
        self.assertTrue(migrated_sidecar.is_file())
        migrated = json.loads(migrated_sidecar.read_text())
        # NOTE: plain assertNotIn(str(self.release_a), ...) would be a false
        # positive here -- "v0.9" is a literal substring of "v0.9.1", so any
        # correctly re-anchored "v0.9.1/..." path also "contains" the v0.9
        # release path as a string. Assert the path-boundary-safe way: the
        # marker must actually live under release_b, not merely mention it.
        self.assertEqual(
            Path(migrated["completion_marker"]).relative_to(self.release_b),
            Path(".dispatch/completion/rt-pfx/plan.json"),
        )


class SymlinkedDataRootRotationSuccessionTest(unittest.TestCase):
    """Round-5 S-3: the review claimed `_succeed_dispatch_state` compares an
    unresolved `candidate` path against a resolved `live_release` path, and
    that a symlinked data root makes the two prefixes silently mismatch. A
    direct reproduction here refutes that: a chain-(3) writer records its
    self-ref path in AGENT_HOME's literal (pointer-form) spelling
    (dispatch_contract.resolve_agent_home's "stored/compared state paths
    must keep pointer form"), and `candidate` carries that exact same
    spelling family, so `_relative_to_release`'s literal match already
    succeeds without resolving `candidate` -- resolving it, as the review's
    suggested fix proposed, actually breaks the match instead (verified by
    reverting to that approach and watching this test fail). This test
    pins the correct, verified behavior: re-anchoring still succeeds
    through a symlinked data root, and a later republish through the
    symlinked (pointer-form) AGENT_HOME spelling is idempotent against the
    resolved spelling the carry-forward wrote, because the idempotent-
    republish check (P-1) compares by resolved identity."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        real_root = self.base / "real_data"
        real_root.mkdir()
        link_root = self.base / "link_data"
        link_root.symlink_to(real_root)

        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ["HARNESS_DATA_ROOT"] = str(link_root)
        self.addCleanup(self._restore_env)

        self.release_a = DISTRIBUTION.data_root() / "releases" / "vA"
        self.release_b = DISTRIBUTION.data_root() / "releases" / "vB"
        for release in (self.release_a, self.release_b):
            (release / "core").mkdir(parents=True)
            (release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text("evidence\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-symroot",
            "route_hash": "h" * 8,
            "registry_digest": "d" * 8,
        }
        self.node = {
            "completion_gate": "artifact",
            "dispatch_depth": 0,
            "execution_surface": "inline",
            "kind": "stage",
        }

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _publish(self, home):
        os.environ["AGENT_HOME"] = str(home)
        # SD-112 chain-3 supersession: see IdempotentRepublishAcrossRotation
        # SuccessionTest -- pin the registry explicitly under `home`.
        os.environ["AGENT_DISPATCH_JOBS"] = str(Path(home) / ".dispatch" / "jobs.log")
        return ROUTE._publish_completion_locked(
            self.route, self.node, "plan", self.evidence,
            attempt_id="att-symroot", attempt_metadata=None,
        )

    def test_republish_after_succession_through_symlinked_data_root(self):
        self._publish(self.release_a)
        sidecar = (
            self.release_a / ".dispatch" / "completion" / "rt-symroot"
            / "plan.att-symroot.attempt.json"
        )
        self.assertTrue(sidecar.is_file())

        current = DISTRIBUTION.current_path()
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(self.release_b)
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.release_a))

        migrated_sidecar = (
            self.release_b / ".dispatch" / "completion" / "rt-symroot"
            / "plan.att-symroot.attempt.json"
        )
        self.assertTrue(migrated_sidecar.is_file())
        migrated = json.loads(migrated_sidecar.read_text())
        # `new_release` (the copy target) is the resolved live release, so
        # the re-anchored value is the resolved spelling, not the symlinked
        # `link_data` pointer form -- readers compare by resolved identity
        # (agent_home_equivalent), so this is the correct outcome, not a
        # literal-spelling match against release_b's unresolved path.
        resolved_marker = Path(migrated["completion_marker"]).resolve(strict=False)
        self.assertEqual(
            resolved_marker,
            (self.release_b.resolve(strict=False) / ".dispatch" / "completion"
             / "rt-symroot" / "plan.json"),
        )

        shutil.rmtree(self.release_a)
        try:
            self._publish(self.release_b)
        except ValueError as exc:
            self.fail(f"republish after rotation succession raised: {exc}")


def _row(ts, state, attempt_id, extra=""):
    """plan.md §3 Phase3 Step3.1 helper: a minimal 6-field registry row."""
    return (
        f"{ts}\t{state}\t/r\t/w\t{attempt_id}\t"
        f"attempt_schema_version=2,registered_worker=1,attempt_id={attempt_id},"
        f"harness=claude{extra}"
    )


class RotationRegistryCarryFidelityTest(unittest.TestCase):
    """plan.md §3 Phase3 Step3.1 (i)+(ii): before this cycle's fix,
    `_succeed_dispatch_state` copied `jobs.log` byte-for-byte -- either
    skipping a live registry outright (carrying nothing forward) or, absent
    a live registry, clobbering nothing but also gaining nothing from a
    later stale-side write. (i) pins that a live terminal row survives
    succession untouched even when the stale side still has that attempt
    open. (ii) reproduces the actual observed defect: a session kept
    writing to the *stale* registry after a first succession pass had
    already frozen a snapshot of it, and a second succession pass could not
    see that later write -- because the destination-exists byte copy makes
    every row after the first succession invisible to every succession
    after it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)

        self.old_release = DISTRIBUTION.data_root() / "releases" / "old"
        self.new_release = DISTRIBUTION.data_root() / "releases" / "new"
        for release in (self.old_release, self.new_release):
            (release / "core").mkdir(parents=True)
            (release / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        current = DISTRIBUTION.current_path()
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(self.new_release)

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_succession_never_reverts_terminal_attempt(self):
        live_jobs = self.new_release / ".dispatch" / "jobs.log"
        live_jobs.parent.mkdir(parents=True, exist_ok=True)
        live_row = _row(
            "2026-08-23T00:00:00Z", "done", "att-X",
            ",note=completed-marker,completion_marker=/tmp/m.json",
        )
        live_jobs.write_text(live_row + "\n", encoding="utf-8")

        stale_jobs = self.old_release / ".dispatch" / "jobs.log"
        stale_jobs.parent.mkdir(parents=True, exist_ok=True)
        stale_open = _row("2026-08-22T00:00:00Z", "open", "att-X")
        stale_q = _row("2026-08-22T00:00:01Z", "done", "att-Q")
        stale_jobs.write_text(stale_open + "\n" + stale_q + "\n", encoding="utf-8")

        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.old_release))

        lines = live_jobs.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, [live_row, stale_q])
        self.assertEqual(lines[0].split("\t")[1], "done")
        self.assertIn("note=completed-marker", lines[0])
        self.assertIn("completion_marker=/tmp/m.json", lines[0])

        before = live_jobs.read_text(encoding="utf-8")
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.old_release))
        self.assertEqual(live_jobs.read_text(encoding="utf-8"), before)

    def test_rotation_carry_recovers_late_close_and_late_rows(self):
        stale_jobs = self.old_release / ".dispatch" / "jobs.log"
        stale_jobs.parent.mkdir(parents=True, exist_ok=True)
        row_y = _row(
            "2026-08-22T00:00:00Z", "open", "att-Y",
            ",dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,fallback_hop=same-harness-headless",
        )
        row_z = _row("2026-08-22T00:00:01Z", "done", "att-Z")
        stale_jobs.write_text(row_y + "\n" + row_z + "\n", encoding="utf-8")

        live_jobs = self.new_release / ".dispatch" / "jobs.log"
        self.assertFalse(live_jobs.is_file())

        # 1st succession -- live is created with both stale rows.
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.old_release))
        self.assertTrue(live_jobs.is_file())
        self.assertEqual(len(live_jobs.read_text(encoding="utf-8").splitlines()), 2)

        # A session that froze `current` before rotation kept writing to the
        # now-stale registry afterward: it closes att-Y and registers a
        # brand-new att-W that no succession pass has ever seen.
        DC.close_attempt_row(
            stale_jobs, "att-Y", "completed-marker",
            evidence={
                "reconcile_reason": "test", "detected_by": "test", "failure_class": "pass",
            },
        )
        row_w = _row("2026-08-22T00:00:02Z", "done", "att-W")
        with stale_jobs.open("a", encoding="utf-8") as handle:
            handle.write(row_w + "\n")

        # 2nd succession -- the next rotation or prune pass's own
        # fail-closed safety net (core/OPERATIONS.md §5.10).
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.old_release))

        lines = live_jobs.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        att_y_lines = [line for line in lines if "attempt_id=att-Y" in line]
        self.assertEqual(len(att_y_lines), 1)
        self.assertEqual(att_y_lines[0].split("\t")[1], "done")
        self.assertEqual(len([line for line in lines if "attempt_id=att-W" in line]), 1)
        self.assertEqual(len([line for line in lines if "attempt_id=att-Z" in line]), 1)

        before = live_jobs.read_text(encoding="utf-8")
        self.assertTrue(DISTRIBUTION._succeed_dispatch_state(self.old_release))
        self.assertEqual(live_jobs.read_text(encoding="utf-8"), before)


class RegistryRepairStaleRowTest(unittest.TestCase):
    """plan.md §3 Phase3 Step3.2 (iii): `repair-stale-row` must close
    exactly the shape of stale row the real registry showed (plan §2.4) --
    marker-backed positive, marker-missing, axis-skew, and
    route-identity-absent negatives -- and must be a strict no-op once a
    row is already terminal or has already been repaired."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        (self.home / "core").mkdir(parents=True)
        (self.home / "core" / "CORE.md").write_text("x\n", encoding="utf-8")
        self.jobs = self.home / ".dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.prior_env = {
            key: os.environ.get(key) for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS")
        }
        os.environ["AGENT_HOME"] = str(self.home)
        # SD-112 chain-3 supersession: the env-less fallback no longer
        # resolves under `self.home` at all, so pin the registry explicitly
        # to the location this fixture already prepared (`self.jobs`) and
        # passes to `dispatch-registry.py --jobs` -- never touch the real
        # developer HOME (C3) via the old checkout-relative fallback.
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _publish(self, route_id, node_id, attempt_id, dispatch_depth=2):
        evidence = self.base / f"evidence-{attempt_id}.md"
        evidence.write_text("evidence\n", encoding="utf-8")
        route = {"route_id": route_id, "route_hash": "h" * 8, "registry_digest": "d" * 8}
        node = {
            "completion_gate": "artifact",
            "dispatch_depth": dispatch_depth,
            "execution_surface": "registered-headless",
            "kind": "stage",
        }
        attempt_metadata = {
            "attempt_schema_version": 2,
            "dispatch_depth": dispatch_depth,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": True,
            "fallback_hop": "same-harness-headless",
        }
        ROUTE._publish_completion_locked(
            route, node, node_id, evidence,
            attempt_id=attempt_id, attempt_metadata=attempt_metadata,
        )

    def _row(self, state, attempt_id, extra=""):
        return _row("2026-08-23T00:00:00Z", state, attempt_id, extra)

    def _run(self, attempt_id, *, apply=False):
        argv = [
            "dispatch-registry.py", "repair-stale-row",
            "--jobs", str(self.jobs), "--attempt", attempt_id,
            "--agent-home", str(self.home),
        ]
        if apply:
            argv.append("--apply")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = REGISTRY.main(argv)
        return code, buffer.getvalue()

    def test_positive_marker_backed_repair(self):
        self._publish("rt-a", "frame", "att-good")
        row = self._row(
            "open", "att-good",
            ",route_id=rt-a,route_node=frame,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,fallback_hop=same-harness-headless,"
            "route_hash=hhhhhhhh,registry_digest=dddddddd,completion_gate=artifact",
        )
        self.jobs.write_text(row + "\n", encoding="utf-8")

        before = self.jobs.read_text(encoding="utf-8")
        code, out = self._run("att-good")
        self.assertEqual(code, 0)
        self.assertIn("would-repair", out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)

        code, out = self._run("att-good", apply=True)
        self.assertEqual(code, 0)
        self.assertIn('"repaired"', out)
        after = self.jobs.read_text(encoding="utf-8")
        self.assertNotEqual(after, before)
        self.assertTrue(after.startswith("2026-08-23T00:00:00Z\tdone\t"))
        self.assertIn("note=completed-marker", after)
        self.assertIn("reconcile_reason=rotation-carry-stale-open", after)
        self.assertIn("detected_by=registry-repair", after)

    def test_negative_marker_missing(self):
        row = self._row(
            "open", "att-nomarker",
            ",route_id=rt-b,route_node=frame,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,fallback_hop=same-harness-headless",
        )
        self.jobs.write_text(row + "\n", encoding="utf-8")
        before = self.jobs.read_text(encoding="utf-8")
        code, out = self._run("att-nomarker")
        self.assertEqual(code, 65)
        self.assertIn("refused:marker-missing", out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)
        self.assertFalse((self.jobs.parent / "repair" / "registry-repair.jsonl").exists())

    def test_negative_axis_skew(self):
        self._publish("rt-c", "frame", "att-skew", dispatch_depth=2)
        row = self._row(
            "open", "att-skew",
            ",route_id=rt-c,route_node=frame,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,fallback_hop=same-harness-headless,"
            "route_hash=hhhhhhhh,registry_digest=dddddddd,completion_gate=artifact",
        )
        self.jobs.write_text(row + "\n", encoding="utf-8")
        before = self.jobs.read_text(encoding="utf-8")
        code, out = self._run("att-skew")
        self.assertEqual(code, 65)
        self.assertIn("refused:axis-skew", out)
        self.assertIn('"dispatch_depth"', out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)

        # No mitigation flag exists for axis skew -- `--apply` alone must
        # not close this row either.
        code, out = self._run("att-skew", apply=True)
        self.assertEqual(code, 65)
        self.assertIn("refused:axis-skew", out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)

    def test_negative_route_identity_absent(self):
        row = self._row("open", "att-owner", ",owner_route_id=rt-owner")
        self.jobs.write_text(row + "\n", encoding="utf-8")
        before = self.jobs.read_text(encoding="utf-8")
        code, out = self._run("att-owner")
        self.assertEqual(code, 65)
        self.assertIn("refused:row-route-identity-absent", out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)

    def test_already_terminal_is_a_no_op(self):
        row = self._row("done", "att-done")
        self.jobs.write_text(row + "\n", encoding="utf-8")
        before = self.jobs.read_text(encoding="utf-8")
        code, out = self._run("att-done")
        self.assertEqual(code, 0)
        self.assertIn("already-terminal", out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)

    def test_repeat_apply_is_idempotent(self):
        self._publish("rt-d", "frame", "att-idem")
        row = self._row(
            "open", "att-idem",
            ",route_id=rt-d,route_node=frame,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,fallback_hop=same-harness-headless,"
            "route_hash=hhhhhhhh,registry_digest=dddddddd,completion_gate=artifact",
        )
        self.jobs.write_text(row + "\n", encoding="utf-8")
        code, _ = self._run("att-idem", apply=True)
        self.assertEqual(code, 0)
        after_first = self.jobs.read_text(encoding="utf-8")

        code, out = self._run("att-idem", apply=True)
        self.assertEqual(code, 0)
        self.assertIn("already-terminal", out)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), after_first)


class RegistryRowIdentityParityTest(unittest.TestCase):
    """plan.md §3 Phase3 Step3.3: `DISTRIBUTION._registry_row_identity` is a
    hand-written stdlib mirror of `DC._row_identity`
    (utilities/dispatch_contract.py). A silent drift between the two would
    let the registry-carry merge (Phase 1) disagree with every other
    reader/writer of these rows about what "the same attempt" means."""

    GRID = [
        ["t", "open", "/r", "/w", "s", "attempt_id=att-1,harness=claude"],
        ["t", "open", "/r", "/w", "s", "route_id=rt-1,route_node=frame,parent=att-owner"],
        ["t", "open", "/r", "/w", "s", "harness=claude,registered_worker=1"],
        ["t", "open", "/r", "/w", "s"],
        ["t", "open", "/r", "/w", "s", "attempt_id=att-1", "extra"],
        ["t", "open", "/r", "/w", "s", "attempt_id=att=weird"],
        ["t", "open", "/r", "/w", "s", ""],
        ["t", "open", "/r", "/w", "s", "attempt_id=att-first,attempt_id=att-second"],
        ["t", "open", "/r", "/w", "s", "attempt_id="],
    ]

    def test_identity_parity_across_grid(self):
        for fields in self.GRID:
            with self.subTest(fields=fields):
                self.assertEqual(
                    DISTRIBUTION._registry_row_identity(fields),
                    DC._row_identity(fields),
                )


class StableStateRootParityTest(unittest.TestCase):
    """SD-112 decision 6: `tools/install/distribution.py`'s standalone
    `stable_state_root()` mirror must resolve to the exact same absolute
    path as `utilities/dispatch_contract.py`'s runtime helper for the same
    env matrix -- the installer cannot import `utilities/` (bootstrap
    constraint), so the two copies are bound together by this fixture
    instead of a shared import."""

    def _matrix(self, base):
        return [
            {"HOME": str(base / "home1")},
            {"HOME": str(base / "home2"), "XDG_STATE_HOME": str(base / "xdg2")},
            {
                "HOME": str(base / "home3"),
                "XDG_STATE_HOME": str(base / "xdg3"),
                "HARNESS_STATE_ROOT": str(base / "hsr3"),
            },
            {"HOME": str(base / "home4"), "HARNESS_STATE_ROOT": str(base / "hsr4")},
        ]

    def test_installer_mirror_matches_runtime_helper_across_env_matrix(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for env in self._matrix(base):
                with self.subTest(env=env):
                    self.assertEqual(
                        DISTRIBUTION.stable_state_root(env),
                        DC.stable_state_root(env),
                    )

    def test_installer_mirror_empty_environ_never_touches_live_home(self):
        with self.assertRaises(DISTRIBUTION.DistributionError):
            DISTRIBUTION.stable_state_root({})
        with self.assertRaises(DC.DispatchContractError):
            DC.stable_state_root({})


class MigrationAliasWriterReaderParityTest(unittest.TestCase):
    """SD-112 §13.33.2-(3)/(4): the record the installer writes at M4 must be
    one the runtime's alias reader actually accepts.

    The two sides live in different modules that cannot import each other, so
    nothing but this fixture stops them from drifting. They did drift once:
    the writer emitted bare hex digests while the reader's contract is the
    repository-wide `sha256:<64 hex>` spelling -- invisible for as long as the
    reader only checked that the field was non-empty.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ["HOME"] = str(self.base / "stable-home")
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _migrate_one_release(self):
        release = DISTRIBUTION.data_root() / "releases" / "v-alias-parity"
        (release / "core").mkdir(parents=True)
        (release / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        legacy_dispatch = release / ".dispatch"
        legacy_dispatch.mkdir(parents=True)
        (legacy_dispatch / "jobs.log").write_text(
            "2026-08-01T00:00:00Z\tdone\t/r\t/w\tatt-alias\t"
            "attempt_schema_version=2,registered_worker=1,attempt_id=att-alias,"
            "harness=claude\n",
            encoding="utf-8",
        )
        result = DISTRIBUTION.run_dispatch_state_migration(
            legacy_dispatch, environ=os.environ
        )
        self.assertEqual(result["status"], "completed")
        return legacy_dispatch / "jobs.log"

    def test_installer_written_record_is_accepted_by_the_runtime_reader(self):
        legacy_jobs = self._migrate_one_release()
        stable_root = DC.stable_state_root(os.environ)
        record = DC.resolve_completed_alias(stable_root, legacy_jobs)
        self.assertIsNotNone(
            record,
            "the M4 record the installer just wrote must pass the runtime's "
            "own structural validation",
        )
        self.assertTrue(DC._alias_record_valid(record))
        for value in (
            record["legacy_jobs_identity"]["content_digest"],
            record["stable_jobs_identity"]["content_digest"],
            record["source_digest"],
            record["target_digest"],
        ):
            self.assertTrue(DC._alias_digest_well_formed(value), value)

    def test_pruned_source_resolves_through_the_written_record(self):
        legacy_jobs = self._migrate_one_release()
        shutil.rmtree(legacy_jobs.parent)
        resolution = DC.resolve_dangling_registry(legacy_jobs, environ=os.environ)
        self.assertEqual(resolution.status, "aliased")
        self.assertEqual(
            resolution.jobs_path, DC.stable_state_root(os.environ) / "jobs.log"
        )


class MigrationAliasRecordValidationTest(unittest.TestCase):
    """SD-112 §13.33.2-(3): `completed` plus a filled-in field is not a
    digest check. Each case below is a record that is *structurally complete*
    in the old sense and must still be refused."""

    def _record(self, **overrides):
        record = {
            "record_version": DC.MIGRATION_ALIAS_RECORD_VERSION,
            "status": "completed",
            "legacy_jobs_identity": {
                "path": "/legacy/.dispatch/jobs.log",
                "content_digest": "sha256:" + "a" * 64,
            },
            "stable_jobs_identity": {
                "path": "/stable/hearting/dispatch/jobs.log",
                "content_digest": "sha256:" + "b" * 64,
            },
            "source_digest": "sha256:" + "c" * 64,
            "target_digest": "sha256:" + "d" * 64,
        }
        record.update(overrides)
        return record

    def test_well_formed_record_is_accepted(self):
        self.assertTrue(DC._alias_record_valid(self._record()))
        self.assertTrue(
            DC._alias_record_valid(
                self._record(route_hash="sha256:" + "e" * 64)
            )
        )

    def test_non_digest_content_digest_is_refused(self):
        for bad in ("x", "sha256:", "sha256:" + "a" * 63, "a" * 64, "SHA256:" + "a" * 64,
                    "sha256:" + "A" * 64, 1, None):
            with self.subTest(bad=bad):
                record = self._record()
                record["stable_jobs_identity"]["content_digest"] = bad
                self.assertFalse(DC._alias_record_valid(record))

    def test_non_digest_tree_digest_is_refused(self):
        for field in ("source_digest", "target_digest"):
            with self.subTest(field=field):
                self.assertFalse(DC._alias_record_valid(self._record(**{field: "x"})))

    def test_relative_or_empty_identity_path_is_refused(self):
        for bad in ("relative/jobs.log", "", None, 7):
            with self.subTest(bad=bad):
                record = self._record()
                record["legacy_jobs_identity"]["path"] = bad
                self.assertFalse(DC._alias_record_valid(record))

    def test_present_but_malformed_route_hash_is_refused(self):
        # Absent is allowed (verified only when present); garbage is not --
        # otherwise a forgery opts out of the extra check for free.
        self.assertFalse(DC._alias_record_valid(self._record(route_hash="nope")))


class MigrationM0ToM4PromotionTest(unittest.TestCase):
    """SD-112 M0-M4 + B-1/B-13b: a full migration run promotes a
    release-embedded `.dispatch` tree into the stable root, and the stable
    registry's `jobs.log` identity (path, device, inode, ctime) survives
    two subsequent rotations that have nothing new to carry -- the
    cycle-2 gate for SD-111."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT", "HARNESS_DATA_ROOT")
        }
        self.stable_home = self.base / "stable-home"
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ["HOME"] = str(self.stable_home)
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _make_release(self, name, ts):
        rel = DISTRIBUTION.data_root() / "releases" / name
        (rel / "core").mkdir(parents=True)
        (rel / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        os.utime(rel, (ts, ts))
        return rel

    def _stable_jobs_observation(self, label, stable_jobs):
        st = stable_jobs.stat()
        return {
            "label": label,
            "jobs_path": str(stable_jobs),
            "st_dev": st.st_dev,
            "st_ino": st.st_ino,
            "st_ctime_ns": st.st_ctime_ns,
        }

    def test_b1_stable_jobs_identity_survives_three_rotations(self):
        import time

        future = time.time() + 60_000_000
        v1 = self._make_release("v-b1-1", future)

        (v1 / ".dispatch").mkdir(parents=True)
        (v1 / ".dispatch" / "jobs.log").write_text(
            "2026-08-01T00:00:00Z\tdone\t/r\t/w\tatt-b1\t"
            "attempt_schema_version=2,registered_worker=1,attempt_id=att-b1,"
            "harness=claude\n",
            encoding="utf-8",
        )
        DISTRIBUTION.current_path().parent.mkdir(parents=True, exist_ok=True)
        DISTRIBUTION.current_path().symlink_to(v1)

        result = DISTRIBUTION.run_dispatch_state_migration(
            v1 / ".dispatch", environ=os.environ
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(DISTRIBUTION._dispatch_migration_promoted(os.environ))

        stable_root = DISTRIBUTION.stable_state_root(os.environ)
        stable_jobs = stable_root / "jobs.log"
        observations = [self._stable_jobs_observation("v1_post_migration", stable_jobs)]

        v2 = self._make_release("v-b1-2", future + 1)
        DISTRIBUTION.current_path().unlink()
        DISTRIBUTION.current_path().symlink_to(v2)
        DISTRIBUTION._cleanup_releases(keep=set())
        # Retention floor: only 2 candidates exist (v1, v2) -- neither is
        # pruned yet, and B-13b's "no new delta" claim needs a call that has
        # nothing left to carry.
        self.assertTrue(v1.exists())
        observations.append(self._stable_jobs_observation("v2_post_rotation", stable_jobs))

        v3 = self._make_release("v-b1-3", future + 2)
        DISTRIBUTION.current_path().unlink()
        DISTRIBUTION.current_path().symlink_to(v3)
        DISTRIBUTION._cleanup_releases(keep=set())
        self.assertFalse(v1.exists(), "v1 should be pruned once retention floor moves past it")
        observations.append(self._stable_jobs_observation("v3_post_rotation", stable_jobs))

        path_same = len({o["jobs_path"] for o in observations}) == 1
        device_inode_same = len({(o["st_dev"], o["st_ino"]) for o in observations}) == 1
        no_recreation = len({o["st_ctime_ns"] for o in observations}) == 1
        stable_text = stable_jobs.read_text(encoding="utf-8")
        stable_lines = [line for line in stable_text.splitlines() if line.strip()]
        owner_terminal = len(stable_lines) == 1 and stable_lines[0].split("\t")[1] == "done"
        stable_digest_preserved = len(stable_lines) == 1

        grade = {
            "result": "pass" if all(
                [path_same, device_inode_same, no_recreation, owner_terminal, stable_digest_preserved]
            ) else "fail",
            "conditions": {
                "path_same": path_same,
                "device_inode_same": device_inode_same,
                "no_recreation": no_recreation,
                "owner_terminal": owner_terminal,
                "stable_digest_preserved": stable_digest_preserved,
            },
        }
        self.assertEqual(grade["result"], "pass", grade)

        # B-13b: promoted _succeed_dispatch_state carries no new delta across
        # the two rotations above -- active-release `.dispatch` never grew.
        self.assertFalse((v2 / ".dispatch" / "jobs.log").exists())
        self.assertFalse((v3 / ".dispatch" / "jobs.log").exists())


class MigrationIdempotencyAndAbortTest(unittest.TestCase):
    """SD-112 B-9/B-11: a copy/verify fault aborts the migration (no
    promotion, no partial canonical file, legacy source unharmed), and a
    second run against the same, unchanged source is a true no-op."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "release" / ".dispatch"
        self.source.mkdir(parents=True)
        (self.source / "jobs.log").write_text(
            "2026-08-01T00:00:00Z\tdone\t/r\t/w\tatt-x\t"
            "attempt_schema_version=2,registered_worker=1,attempt_id=att-x,"
            "harness=claude\n",
            encoding="utf-8",
        )
        (self.source / "logs").mkdir()
        (self.source / "logs" / "note.txt").write_text("hello\n", encoding="utf-8")
        self.env = {"HOME": str(self.base / "home")}

    def test_idempotent_rerun_copies_nothing_and_stays_completed(self):
        first = DISTRIBUTION.run_dispatch_state_migration(self.source, environ=self.env)
        self.assertEqual(first["status"], "completed")
        stable_root = DISTRIBUTION.stable_state_root(self.env)
        note = stable_root / "logs" / "note.txt"
        before_stat = note.stat()

        second = DISTRIBUTION.run_dispatch_state_migration(self.source, environ=self.env)
        self.assertEqual(second["status"], "already-completed")
        after_stat = note.stat()
        self.assertEqual(before_stat.st_ino, after_stat.st_ino)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

        journal = DISTRIBUTION._read_migration_journal(stable_root)
        completed = [r for r in journal if r.get("status") == "completed"]
        self.assertEqual(len(completed), 1)

    def test_copy_fault_aborts_without_promotion_or_partial_file(self):
        # Sabotage the copy destination the same way the existing T-2
        # rotation fixture does: pre-create the target file's parent as a
        # plain file so mkdir(parents=True) raises inside the copy loop.
        env = {"HOME": str(self.base / "home-fault")}
        stable_root = DISTRIBUTION.stable_state_root(env)
        stable_root.mkdir(parents=True)
        os.chmod(stable_root, 0o700)
        (stable_root / "logs").write_text("not a directory\n", encoding="utf-8")

        result = DISTRIBUTION.run_dispatch_state_migration(self.source, environ=env)
        self.assertEqual(result["status"], "aborted")
        self.assertFalse(DISTRIBUTION._dispatch_migration_promoted(env))

        journal = DISTRIBUTION._read_migration_journal(stable_root)
        statuses = [r.get("status") for r in journal]
        self.assertIn("aborted", statuses)
        self.assertNotIn("completed", statuses)

        # No partial canonical registry -- the additive copy loop never
        # reaches jobs.log's own merge step's aftermath being mistaken for
        # a completed file; the legacy source itself must be untouched.
        self.assertTrue((self.source / "jobs.log").is_file())
        self.assertIn("att-x", (self.source / "jobs.log").read_text(encoding="utf-8"))


class StableRegistrySnapshotAndDeletionPreconditionTest(unittest.TestCase):
    """SD-112 §13.33.2-(5)/decision 2: the stable registry's third
    reference-registry source and the post-promotion deletion precondition.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = {"HOME": str(self.base / "home")}
        self.stable_root = DISTRIBUTION.stable_state_root(self.env)

    def _row(self, status, attempt_id, launch_home=None):
        pipe = f"attempt_id={attempt_id},harness=claude"
        if launch_home is not None:
            pipe += f",launch_home={launch_home}"
        return f"2026-08-01T00:00:00Z\t{status}\t/r\t/w\t{attempt_id}\t{pipe}"

    def test_missing_stable_registry_is_an_empty_snapshot(self):
        self.assertEqual(DISTRIBUTION._stable_registry_snapshot(self.env), [])

    def test_unreadable_malformed_stable_registry_raises_typed_refusal(self):
        self.stable_root.mkdir(parents=True)
        (self.stable_root / "jobs.log").write_text("not\tenough\tfields\n", encoding="utf-8")
        with self.assertRaises(DISTRIBUTION.DistributionError) as ctx:
            DISTRIBUTION._stable_registry_snapshot(self.env)
        self.assertIn("registry-unreadable", str(ctx.exception))

    def test_duplicate_and_conflicting_rows_normalize_per_decision_2(self):
        self.stable_root.mkdir(parents=True)
        candidate = self.base / "candidate-release"
        candidate.mkdir()
        byte_identical = self._row("open", "att-dup", str(candidate))
        open_then_done = [
            self._row("open", "att-term", str(candidate)),
            self._row("done", "att-term", str(candidate)),
        ]
        conflicting_open = [
            self._row("open", "att-conflict", str(candidate)),
            self._row("open", "att-conflict", str(self.base / "other-release")),
        ]
        (self.stable_root / "jobs.log").write_text(
            "\n".join(
                [byte_identical, byte_identical] + open_then_done + conflicting_open
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(DISTRIBUTION.DistributionError):
            DISTRIBUTION._stable_registry_snapshot(self.env)

        # Isolate the two normalization rules that do NOT involve a
        # conflict, without the conflicting rows above short-circuiting them.
        (self.stable_root / "jobs.log").write_text(
            "\n".join([byte_identical, byte_identical] + open_then_done) + "\n",
            encoding="utf-8",
        )
        snapshot = DISTRIBUTION._stable_registry_snapshot(self.env)
        attempt_ids = {entry["fields"][4] for entry in snapshot}
        # att-dup collapses to one open row; att-term's terminal `done` row
        # wins over its earlier `open` row and is excluded from the open set.
        self.assertEqual(attempt_ids, {"att-dup"})

    def test_stable_open_row_with_launch_home_blocks_candidate_deletion(self):
        self.stable_root.mkdir(parents=True)
        candidate = self.base / "candidate-release"
        candidate.mkdir()
        (self.stable_root / "jobs.log").write_text(
            self._row("open", "att-live", str(candidate)) + "\n", encoding="utf-8"
        )
        snapshot = DISTRIBUTION._stable_registry_snapshot(self.env)
        in_use, why = DISTRIBUTION._release_in_use(candidate, snapshot)
        self.assertTrue(in_use)
        self.assertIn("open-attempt:att-live", why)

    def test_stable_open_row_without_launch_home_does_not_retain(self):
        self.stable_root.mkdir(parents=True)
        candidate = self.base / "candidate-release"
        candidate.mkdir()
        (self.stable_root / "jobs.log").write_text(
            self._row("open", "att-unscoped") + "\n", encoding="utf-8"
        )
        snapshot = DISTRIBUTION._stable_registry_snapshot(self.env)
        in_use, _why = DISTRIBUTION._release_in_use(candidate, snapshot)
        self.assertFalse(in_use)

    def test_deletion_precondition_blocks_on_unreconciled_delta(self):
        candidate = self.base / "candidate-release"
        (candidate / ".dispatch" / "logs").mkdir(parents=True)
        (candidate / ".dispatch" / "logs" / "late-write.txt").write_text(
            "late\n", encoding="utf-8"
        )
        self.stable_root.mkdir(parents=True)
        (self.stable_root / "migration-journal.jsonl").write_text(
            json.dumps({
                "record_version": 1,
                "migration_id": "fixture",
                "status": "completed",
                "legacy_jobs_identity": {"path": "/nonexistent/jobs.log", "content_digest": "d"},
                "stable_jobs_identity": {"path": str(self.stable_root / "jobs.log"), "content_digest": "d"},
                "source_digest": "s",
                "target_digest": "t",
            }) + "\n",
            encoding="utf-8",
        )
        ok, reason = DISTRIBUTION._migration_deletion_precondition(candidate, self.env)
        self.assertFalse(ok)
        self.assertIn("dispatch-state-migration-blocked-live-attempt", reason)

    def test_deletion_precondition_passes_once_delta_reconciled(self):
        candidate = self.base / "candidate-release"
        (candidate / ".dispatch" / "logs").mkdir(parents=True)
        source_file = candidate / ".dispatch" / "logs" / "late-write.txt"
        source_file.write_text("late\n", encoding="utf-8")
        self.stable_root.mkdir(parents=True)
        target_file = self.stable_root / "logs" / "late-write.txt"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("late\n", encoding="utf-8")
        (self.stable_root / "migration-journal.jsonl").write_text(
            json.dumps({
                "record_version": 1,
                "migration_id": "fixture",
                "status": "completed",
                "legacy_jobs_identity": {"path": "/nonexistent/jobs.log", "content_digest": "d"},
                "stable_jobs_identity": {"path": str(self.stable_root / "jobs.log"), "content_digest": "d"},
                "source_digest": "s",
                "target_digest": "t",
            }) + "\n",
            encoding="utf-8",
        )
        ok, reason = DISTRIBUTION._migration_deletion_precondition(candidate, self.env)
        self.assertTrue(ok, reason)


class PostPromotionPruneAliasIntegrationTest(unittest.TestCase):
    """SD-112 B-3/B-14 rotation legs (installer side): after a real M0-M4
    promotion AND a real `_cleanup_releases` prune of the source release,
    the completed alias record must let both `capability-route.py`'s
    continuation resolver (`resolve_dangling_registry`, B-3) and Fleet's row
    reader (`_row_roots_for_registry`, B-14) reach the exact same stable
    canonical row -- one root, no duplicates, no stale reference to the
    now-deleted release."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT", "HARNESS_DATA_ROOT")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ["HOME"] = str(self.base / "stable-home")
        os.environ["XDG_STATE_HOME"] = str(self.base / "stable-home" / ".local" / "state")
        os.environ["HARNESS_DATA_ROOT"] = str(self.base / "data")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self.prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_alias_reaches_one_stable_row_after_migration_and_prune(self):
        import time

        future = time.time() + 80_000_000
        v1 = DISTRIBUTION.data_root() / "releases" / "v-b3b14-1"
        (v1 / "core").mkdir(parents=True)
        (v1 / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        os.utime(v1, (future, future))

        legacy_jobs = v1 / ".dispatch" / "jobs.log"
        legacy_jobs.parent.mkdir(parents=True)
        legacy_jobs.write_text(
            "2026-08-01T00:00:00Z\tdone\t/r\t/w\tatt-b3b14\t"
            "attempt_schema_version=2,registered_worker=1,attempt_id=att-b3b14,"
            "route_id=rt-b3b14,harness=claude\n",
            encoding="utf-8",
        )
        completion_dir = v1 / ".dispatch" / "completion" / "rt-b3b14"
        completion_dir.mkdir(parents=True)
        (completion_dir / "plan.json").write_text(
            json.dumps({"schema_version": 2, "route_id": "rt-b3b14", "node_id": "plan"}),
            encoding="utf-8",
        )

        DISTRIBUTION.current_path().parent.mkdir(parents=True, exist_ok=True)
        DISTRIBUTION.current_path().symlink_to(v1)

        result = DISTRIBUTION.run_dispatch_state_migration(v1 / ".dispatch", environ=os.environ)
        self.assertEqual(result["status"], "completed")
        stable_root = DISTRIBUTION.stable_state_root(os.environ)
        self.assertTrue((stable_root / "completion" / "rt-b3b14" / "plan.json").is_file())

        v2 = DISTRIBUTION.data_root() / "releases" / "v-b3b14-2"
        (v2 / "core").mkdir(parents=True)
        (v2 / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        os.utime(v2, (future + 1, future + 1))
        v3 = DISTRIBUTION.data_root() / "releases" / "v-b3b14-3"
        (v3 / "core").mkdir(parents=True)
        (v3 / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        os.utime(v3, (future + 2, future + 2))
        DISTRIBUTION.current_path().unlink()
        DISTRIBUTION.current_path().symlink_to(v3)
        DISTRIBUTION._cleanup_releases(keep=set())
        self.assertFalse(v1.exists(), "v1 must actually be pruned for this to be a real B-3/B-14 test")

        # B-3: capability-route's continuation resolver reaches the stable
        # row through the alias, not the now-deleted release path.
        resolution = DC.resolve_dangling_registry(legacy_jobs, environ=os.environ)
        self.assertEqual(resolution.status, "aliased")
        self.assertEqual(resolution.jobs_path, stable_root / "jobs.log")

        # B-14: Fleet's row reader collapses to exactly the alias target,
        # one root, not the stale candidate list.
        roots = FLEET_ROUTE._row_roots_for_registry(
            [str(legacy_jobs.parent)], str(legacy_jobs)
        )
        self.assertEqual(roots, [str(stable_root)])


if __name__ == "__main__":
    unittest.main()
