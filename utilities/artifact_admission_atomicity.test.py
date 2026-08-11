from __future__ import annotations

"""Phase 0 falsification experiment + lock/recovery/atomicity regression.

Runs against the real NFS artifact root when possible (see `_scratch_root()`);
NFS-specific assertions are `skipTest`-marked, never silently skipped, when the
resolved scratch root turns out to be a local filesystem.
"""

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm
import artifact_identity as idm
import artifact_manifest as m

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_artifact_root() -> Path:
    script = _REPO_ROOT / "utilities" / "artifact-root.sh"
    if script.is_file():
        try:
            out = subprocess.check_output(["bash", str(script)], cwd=str(_REPO_ROOT))
            resolved = out.decode("utf-8").strip()
            if resolved:
                return Path(resolved)
        except Exception:
            pass
    return _REPO_ROOT / ".agent_reports"


_ARTIFACT_ROOT = _resolve_artifact_root()


def _fs_type(path: Path) -> str:
    try:
        out = subprocess.check_output(["stat", "-f", "-c", "%T", str(path)])
        return out.decode("utf-8").strip()
    except Exception:
        return "unknown"


def _scratch_root():
    """Returns (path, is_local_fallback)."""
    env_root = os.environ.get("ARTIFACT_ADMISSION_TEST_ROOT")
    if env_root:
        Path(env_root).mkdir(parents=True, exist_ok=True)
        return Path(env_root), False

    if _ARTIFACT_ROOT.is_dir():
        selftest = _ARTIFACT_ROOT / ".runtime" / "_selftest" / "{0}-{1}".format(
            os.getpid(), int(time.time() * 1000) % 1000000
        )
        try:
            selftest.mkdir(parents=True)
            return selftest, False
        except OSError:
            pass

    return Path(tempfile.mkdtemp(prefix="artifact-admission-selftest-")), True


def _make_valid_document(alloc, identity, *, camp_id=None, cyc_id=None, content=b"hello"):
    camp_id = camp_id or alloc.allocate("campaign")
    cyc_id = cyc_id or alloc.allocate("cycle")
    art_id = alloc.allocate("artifact")
    arev_id = alloc.allocate("artifact_revision")
    man_id = alloc.allocate("manifest")
    mrev_id = alloc.allocate("manifest_revision")
    prod_id = alloc.allocate("producer")
    evt_id = alloc.allocate("event")
    strm_id = alloc.allocate("stream")
    digest = m.digest_bytes(content)
    doc = {
        "schema_version": 2,
        "manifest_kind": "artifact.cycle",
        "manifest_id": man_id,
        "manifest_revision_id": mrev_id,
        "repository_id": identity.repository_id,
        "artifact_root_id": identity.artifact_root_id,
        "campaign": {
            "campaign_id": camp_id,
            "goal": "g",
            "completion_criterion": {"statement": "s"},
            "title": "t",
            "state": "active",
        },
        "cycle": {
            "cycle_id": cyc_id,
            "campaign_id": camp_id,
            "parent_cycle_id": None,
            "started_on": "2026-08-11T00:00:00Z",
            "input_digest": "sha256:" + "0" * 64,
            "outcome_criterion": {"required_artifact_roles": [], "decision_required": False},
            "state": "active",
        },
        "artifacts": [
            {
                "artifact_id": art_id,
                "cycle_id": cyc_id,
                "role": "primary",
                "type": "doc",
                "capability": "autopilot-code",
                "title": "t",
            }
        ],
        "artifact_revisions": [
            {
                "artifact_revision_id": arev_id,
                "artifact_id": art_id,
                "revision_sequence": 1,
                "content_digest": digest,
                "byte_size": len(content),
                "media_type": "text/plain",
                "locator": {"kind": "cycle-relative", "path": "plan.md"},
                "provenance": {
                    "source_manifest_id": man_id,
                    "source_revision_id": mrev_id,
                    "producer_route_id": "r",
                    "algorithm_version": "v1",
                    "schema_version": 1,
                    "source_digest": "sha256:" + "2" * 64,
                },
            }
        ],
        "shared_references": [],
        "shared_reference_revisions": [],
        "routes": [],
        "events": [
            {
                "event_id": evt_id,
                "stream_id": strm_id,
                "stream_sequence": 1,
                "event_type": "artifact.revision.recorded",
                "target_id": art_id,
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:00:00Z",
                "provenance": {
                    "source_manifest_id": man_id,
                    "source_revision_id": mrev_id,
                    "producer_route_id": "r",
                    "algorithm_version": "v1",
                    "schema_version": 1,
                    "source_digest": "sha256:" + "6" * 64,
                },
                "evidence_ids": [],
                "payload": {},
            }
        ],
        "producer": {
            "producer_id": prod_id,
            "contract_version": "artifact-cycle-manifest/v2",
            "source_revision": "abc",
        },
    }
    return doc, content


class AtomicityTestBase(unittest.TestCase):
    def setUp(self):
        self.scratch, self.is_local_fallback = _scratch_root()
        self.root = self.scratch / "root"
        self.root.mkdir(parents=True, exist_ok=True)
        self.fs_type = _fs_type(self.scratch)
        self.identity = adm.ensure_root_identity(
            self.root, allocator=idm.IdAllocator(entropy=lambda n: b"\x22" * n)
        )
        self.alloc = idm.IdAllocator()

    def tearDown(self):
        shutil.rmtree(str(self.scratch), ignore_errors=True)

    def _stage_source(self, content: bytes) -> Path:
        src = self.scratch / "src-{0}".format(idm.IdAllocator().allocate("evidence"))
        src.mkdir()
        (src / "plan.md").write_bytes(content)
        return src


class TestEnvironmentFacts(AtomicityTestBase):
    def test_environment_records_filesystem_facts(self):
        probe_dir = self.scratch / "probe"
        probe_dir.mkdir()

        # mkdir EEXIST
        os.mkdir(str(probe_dir / "d"))
        with self.assertRaises(FileExistsError):
            os.mkdir(str(probe_dir / "d"))

        # O_CREAT|O_EXCL EEXIST
        fd = os.open(str(probe_dir / "f"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        with self.assertRaises(FileExistsError):
            os.open(str(probe_dir / "f"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)

        # os.link EEXIST
        with self.assertRaises(FileExistsError):
            os.link(str(probe_dir / "f"), str(probe_dir / "f"))

        # rename-onto-empty-dir: record only, do not assert (F-1 is that this
        # silently REPLACES on this root -- other filesystems may differ).
        target_empty = probe_dir / "target-empty"
        target_empty.mkdir()
        source_dir = probe_dir / "source"
        source_dir.mkdir()
        rename_replaced = None
        try:
            os.rename(str(source_dir), str(target_empty))
            rename_replaced = True
        except OSError:
            rename_replaced = False

        st_dev = os.stat(str(self.scratch)).st_dev
        record = {
            "fs_type": self.fs_type,
            "is_local_fallback": self.is_local_fallback,
            "scratch_root": str(self.scratch),
            "st_dev": st_dev,
            "rename_onto_empty_dir_replaced": rename_replaced,
        }
        sys.stderr.write("ENV-FACTS: {0}\n".format(json.dumps(record)))
        # The publish path must not depend on rename refusing an empty target
        # directory: on this class of filesystem the observed behaviour can be
        # a silent replace (F-1). The EEXIST-class primitives asserted above
        # are the actual exclusivity substrate, and the admission-level defence
        # is test_admit_does_not_replace_existing_empty_canonical_directory.
        self.assertIsNotNone(record["rename_onto_empty_dir_replaced"])
        self.assertIsInstance(record["rename_onto_empty_dir_replaced"], bool)


class TestStagingDevice(AtomicityTestBase):
    def test_staging_is_same_device_as_publish_target(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        publish_target, deepest_parent, staging = adm.publish_plan(
            self.root, doc["campaign"]["campaign_id"], doc["cycle"]["cycle_id"]
        )
        self.assertIsNotNone(staging)
        self.assertEqual(staging.parent, deepest_parent)
        self.assertEqual(
            os.stat(str(deepest_parent)).st_dev, os.stat(str(self.root)).st_dev
        )


_CHILD_ADMIT_SCRIPT = textwrap.dedent(
    """
    import sys, json, os
    sys.path.insert(0, {utilities_dir!r})
    import artifact_admission as adm

    root = {root!r}
    document = json.loads({document_json!r})
    src = {src!r}
    key = {key!r}
    barrier = {barrier!r}

    if barrier:
        while not os.path.exists(barrier):
            pass

    req = adm.AdmissionRequest(idempotency_key=key, document=document, staging_source=src)
    try:
        outcome = adm.admit(root, req, lock_timeout=20.0)
        result = {{"status": outcome.status, "cycle_path": outcome.cycle_path,
                   "violations": [v.to_payload() for v in outcome.violations]}}
    except Exception as exc:
        result = {{"status": "exception", "error": "{{0}}: {{1}}".format(type(exc).__name__, exc)}}
    sys.stdout.write(json.dumps(result))
    """
)


def _run_child_admit(utilities_dir, root, document, src, key, barrier=None, kill_after=None):
    script = _CHILD_ADMIT_SCRIPT.format(
        utilities_dir=str(utilities_dir),
        root=str(root),
        document_json=json.dumps(document),
        src=str(src) if src else None,
        key=key,
        barrier=str(barrier) if barrier else None,
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


class TestConcurrentAdmission(AtomicityTestBase):
    def test_concurrent_same_identity_yields_one_folder(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        utilities_dir = Path(__file__).resolve().parent
        barrier = self.scratch / "go"

        p1 = _run_child_admit(utilities_dir, self.root, doc, src, doc["manifest_id"], barrier=barrier)
        p2 = _run_child_admit(utilities_dir, self.root, doc, src, doc["manifest_id"], barrier=barrier)
        time.sleep(0.2)
        barrier.write_text("go")

        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)

        r1 = json.loads(out1.decode("utf-8"))
        r2 = json.loads(out2.decode("utf-8"))

        statuses = sorted([r1["status"], r2["status"]])
        # Exactly one process may admit; the mutex makes the loser observe the
        # winner's manifest and report the idempotent no-op. A double
        # "admitted" is the broken-mutex signature and must fail loudly.
        self.assertEqual(statuses, ["admitted", "noop-idempotent"])

        cycle_dir = self.root / "campaigns" / doc["campaign"]["campaign_id"] / "cycles" / doc["cycle"]["cycle_id"]
        self.assertTrue(cycle_dir.is_dir())
        campaign_dir = self.root / "campaigns" / doc["campaign"]["campaign_id"]
        cycles_present = [
            p for p in (campaign_dir / "cycles").iterdir()
        ]
        self.assertEqual(len(cycles_present), 1)

        campaigns_dir = self.root / "campaigns"
        residue = [p.name for p in campaigns_dir.rglob(".admitting-*")]
        self.assertEqual(residue, [])

        index = adm.load_index(self.root)
        self.assertEqual(len(index.cycles), 1)

    def test_concurrent_distinct_identity_both_admit(self):
        doc1, content1 = _make_valid_document(self.alloc, self.identity)
        doc2, content2 = _make_valid_document(self.alloc, self.identity)
        src1 = self._stage_source(content1)
        src2 = self._stage_source(content2)
        utilities_dir = Path(__file__).resolve().parent
        barrier = self.scratch / "go"

        p1 = _run_child_admit(utilities_dir, self.root, doc1, src1, doc1["manifest_id"], barrier=barrier)
        p2 = _run_child_admit(utilities_dir, self.root, doc2, src2, doc2["manifest_id"], barrier=barrier)
        time.sleep(0.2)
        barrier.write_text("go")

        out1, _ = p1.communicate(timeout=30)
        out2, _ = p2.communicate(timeout=30)
        r1 = json.loads(out1.decode("utf-8"))
        r2 = json.loads(out2.decode("utf-8"))
        self.assertEqual(r1["status"], "admitted")
        self.assertEqual(r2["status"], "admitted")

        index = adm.load_index(self.root)
        self.assertEqual(len(index.cycles), 2)

    def test_concurrent_conflicting_route_composite_only_one_admits(self):
        doc1, content1 = _make_valid_document(self.alloc, self.identity)
        doc2, content2 = _make_valid_document(self.alloc, self.identity)
        route = {
            "artifact_root_id": self.identity.artifact_root_id,
            "route_id": "rt-shared-1",
            "route_hash": "sha256:" + "9" * 64,
            "terminal_marker": "m",
            "terminal_evidence_id": doc1["events"][0]["event_id"],
        }
        doc1["routes"] = [dict(route)]
        route2 = dict(route)
        route2["terminal_evidence_id"] = doc2["events"][0]["event_id"]
        doc2["routes"] = [route2]

        src1 = self._stage_source(content1)
        src2 = self._stage_source(content2)
        utilities_dir = Path(__file__).resolve().parent
        barrier = self.scratch / "go"

        p1 = _run_child_admit(utilities_dir, self.root, doc1, src1, doc1["manifest_id"], barrier=barrier)
        p2 = _run_child_admit(utilities_dir, self.root, doc2, src2, doc2["manifest_id"], barrier=barrier)
        time.sleep(0.2)
        barrier.write_text("go")

        out1, _ = p1.communicate(timeout=30)
        out2, _ = p2.communicate(timeout=30)
        r1 = json.loads(out1.decode("utf-8"))
        r2 = json.loads(out2.decode("utf-8"))
        statuses = sorted([r1["status"], r2["status"]])
        self.assertEqual(statuses, ["admitted", "rejected"])


class TestCrashRecovery(AtomicityTestBase):
    def test_kill_after_stage_before_publish_rolls_back_and_commits_nothing(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        utilities_dir = Path(__file__).resolve().parent

        script = textwrap.dedent(
            """
            import sys, json, os
            sys.path.insert(0, {utilities_dir!r})
            import artifact_admission as adm
            root = {root!r}
            document = json.loads({document_json!r})
            src = {src!r}
            key = {key!r}

            def _kill_before_rename(src_path, dst_path):
                os.kill(os.getpid(), 9)
            adm.os.rename = _kill_before_rename

            req = adm.AdmissionRequest(idempotency_key=key, document=document, staging_source=src)
            adm.admit(root, req)
            """
        ).format(
            utilities_dir=str(utilities_dir),
            root=str(self.root),
            document_json=json.dumps(doc),
            src=str(src),
            key=doc["manifest_id"],
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -9)

        cycle_dir = self.root / "campaigns" / doc["campaign"]["campaign_id"] / "cycles" / doc["cycle"]["cycle_id"]
        self.assertFalse(cycle_dir.exists())

        # Recovery: quarantine and no commit.
        result = adm.recover(self.root)
        self.assertIn(doc["manifest_id"], result["rolled_back"])
        self.assertFalse(cycle_dir.exists())
        quarantine_dir = self.root / adm.ADMISSION_REL / "quarantine"
        self.assertTrue(quarantine_dir.is_dir())

    def test_kill_after_publish_before_index_rolls_forward(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        utilities_dir = Path(__file__).resolve().parent

        script = textwrap.dedent(
            """
            import sys, json, os
            sys.path.insert(0, {utilities_dir!r})
            import artifact_admission as adm
            root = {root!r}
            document = json.loads({document_json!r})
            src = {src!r}
            key = {key!r}

            orig_write_journal = adm._write_journal
            def _kill_on_published(root_, idem, **fields):
                orig_write_journal(root_, idem, **fields)
                if fields.get("state") == "published":
                    os.kill(os.getpid(), 9)
            adm._write_journal = _kill_on_published

            req = adm.AdmissionRequest(idempotency_key=key, document=document, staging_source=src)
            adm.admit(root, req)
            """
        ).format(
            utilities_dir=str(utilities_dir),
            root=str(self.root),
            document_json=json.dumps(doc),
            src=str(src),
            key=doc["manifest_id"],
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -9)

        cycle_dir = self.root / "campaigns" / doc["campaign"]["campaign_id"] / "cycles" / doc["cycle"]["cycle_id"]
        self.assertTrue((cycle_dir / "manifest.json").is_file())

        index_before = adm.load_index(self.root)
        self.assertEqual(len(index_before.cycles), 0)

        result = adm.recover(self.root)
        self.assertIn(doc["manifest_id"], result["rolled_forward"])

        index_after = adm.load_index(self.root)
        self.assertEqual(len(index_after.cycles), 1)

    def test_recovery_converges_in_one_direction_only(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        req = adm.AdmissionRequest(idempotency_key=doc["manifest_id"], document=doc, staging_source=src)
        out = adm.admit(self.root, req)
        self.assertEqual(out.status, "admitted")
        # calling recover repeatedly on an already-committed state is a no-op.
        r1 = adm.recover(self.root)
        r2 = adm.recover(self.root)
        self.assertEqual(r1, {"rolled_forward": [], "rolled_back": []})
        self.assertEqual(r2, {"rolled_forward": [], "rolled_back": []})

    def test_staging_residue_never_appears_under_canonical(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        req = adm.AdmissionRequest(idempotency_key=doc["manifest_id"], document=doc, staging_source=src)
        adm.admit(self.root, req)
        residue = list((self.root / "campaigns").rglob(".admitting-*"))
        self.assertEqual(residue, [])

    def test_rolled_back_staging_is_quarantined_not_deleted(self):
        doc, content = _make_valid_document(self.alloc, self.identity)
        src = self._stage_source(content)
        utilities_dir = Path(__file__).resolve().parent
        script = textwrap.dedent(
            """
            import sys, json, os
            sys.path.insert(0, {utilities_dir!r})
            import artifact_admission as adm
            root = {root!r}
            document = json.loads({document_json!r})
            src = {src!r}
            key = {key!r}
            def _kill_before_rename(src_path, dst_path):
                os.kill(os.getpid(), 9)
            adm.os.rename = _kill_before_rename
            req = adm.AdmissionRequest(idempotency_key=key, document=document, staging_source=src)
            adm.admit(root, req)
            """
        ).format(
            utilities_dir=str(utilities_dir),
            root=str(self.root),
            document_json=json.dumps(doc),
            src=str(src),
            key=doc["manifest_id"],
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        proc.wait(timeout=30)
        adm.recover(self.root)
        quarantine_dir = self.root / adm.ADMISSION_REL / "quarantine"
        entries = list(quarantine_dir.iterdir()) if quarantine_dir.is_dir() else []
        self.assertTrue(len(entries) >= 1)
        found_manifest = any((e / "manifest.json").exists() or any(e.rglob("manifest.json")) for e in entries)
        self.assertTrue(found_manifest)


class TestLockContract(AtomicityTestBase):
    def test_live_holder_lock_raises_admission_busy(self):
        fd = adm._acquire_lock(self.root, timeout=10.0)
        try:
            with self.assertRaises(adm.AdmissionBusy):
                adm._acquire_lock(self.root, timeout=0.3)
        finally:
            adm._release_lock(self.root, fd)

    def test_dead_holder_lock_is_reclaimed(self):
        lock_dir = adm._lock_dir(self.root)
        lock_dir.mkdir(parents=True)
        fake_pid = 999999
        while os.path.exists("/proc/{0}".format(fake_pid)):
            fake_pid -= 1
        holder = {
            "pid": fake_pid,
            "host": adm.socket.gethostname(),
            "proc_start_ticks": 1,
            "acquired_at": time.time(),
            "attempt": "dead-holder",
            "boot_id": adm._boot_id(),
        }
        adm._create_exclusive_json(lock_dir / "holder.json", holder)
        fd = adm._acquire_lock(self.root, timeout=5.0)
        try:
            # one-way migration: the dead legacy mkdir-lock was cleared under
            # the flock's exclusivity.
            self.assertFalse(lock_dir.exists())
        finally:
            adm._release_lock(self.root, fd)

    def test_flock_released_on_holder_death(self):
        """F5 regression: a killed holder releases the mutex automatically.

        With the OS advisory lock there is no dead-holder record to reclaim,
        so the two-waiter reclamation race of the mkdir mutex cannot exist.
        """
        code = (
            "import os, sys, time\n"
            "sys.path.insert(0, {0!r})\n"
            "import artifact_admission as adm\n"
            "fd = adm._acquire_lock({1!r}, timeout=5.0)\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(60)\n"
        ).format(str(Path(__file__).resolve().parent), str(self.root))
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE
        )
        try:
            line = child.stdout.readline().decode("utf-8").strip()
            self.assertEqual(line, "LOCKED")
            with self.assertRaises(adm.AdmissionBusy):
                adm._acquire_lock(self.root, timeout=0.3)
            child.kill()
            child.wait(timeout=10)
            fd = adm._acquire_lock(self.root, timeout=5.0)
            adm._release_lock(self.root, fd)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_pid_reuse_does_not_steal_live_lock(self):
        holder = {
            "pid": os.getpid(),
            "host": adm.socket.gethostname(),
            "proc_start_ticks": adm._proc_start_ticks(os.getpid()) + 1000,
            "acquired_at": time.time(),
            "attempt": "stale-record",
            "boot_id": adm._boot_id(),
        }
        self.assertFalse(adm._pid_is_live(holder))


if __name__ == "__main__":
    unittest.main(verbosity=2)
