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
import importlib.util
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
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)

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
            (self.release / ".dispatch" / "completion" / "rt-p1").glob(
                "plan.att-*.attempt.json"
            )
        )
        self.assertEqual(len(sidecars), 1)
        original_bytes = sidecars[0].read_bytes()
        recorded = json.loads(original_bytes)
        # Chain-3 with a symlinked AGENT_HOME records pointer form.
        self.assertIn(str(self.current), recorded["completion_marker"])

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
        self.prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS")
        }
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
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
            self.release / ".dispatch" / "completion" / "rt-gm1"
            / "plan.att-gm1.attempt.json"
        )
        self.assertTrue(sidecar.is_file())
        recorded = json.loads(sidecar.read_text())
        # Chain-3 with a symlinked AGENT_HOME records pointer form.
        self.assertIn(str(self.current), recorded["completion_marker"])

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


if __name__ == "__main__":
    unittest.main()
