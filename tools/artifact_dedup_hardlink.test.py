#!/usr/bin/env python3
"""Tests for tools/artifact-dedup-hardlink.py (plan -> apply -> verify -> revert)."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "artifact-dedup-hardlink.py"
spec = importlib.util.spec_from_file_location("dedup_hardlink", TOOL)
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

OLD = time.time() - 3 * 86400
REF = "ref_" + "a" * 32


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def cyc(n):
    return "cyc_" + f"{n:032x}"


class Store:
    """A minimal artifact root: one shared spec reference and sealed cycles."""

    def __init__(self, base):
        self.root = Path(base) / ".agent_reports"
        self.root.mkdir()
        (self.root / ".runtime/artifact-producer/v1/cycles").mkdir(parents=True)
        (self.root / ".runtime/routes").mkdir(parents=True)
        self.revisions = []

    def write(self, rel, data, mtime=OLD):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return path

    def revision(self, rid, files):
        rows = []
        for name, data in files.items():
            self.write(f"shared/spec/{REF}/revisions/{rid}/{name}", data)
            rows.append({"path": name, "sha256": sha(data), "byte_size": len(data)})
        self.write(f"shared/spec/{REF}/revisions/{rid}/revision.json", json.dumps(
            {"shared_reference_id": REF, "shared_reference_revision_id": rid, "files": rows}).encode())
        self.revisions.append(rid)
        self.write(f"shared/spec/{REF}/reference.json", json.dumps(
            {"shared_reference_id": REF, "revisions": self.revisions, "latest_revision_id": rid}).encode())

    def cycle(self, n, files, *, state="sealed", closed=True, digests=None, route=None):
        cycle_id = cyc(n)
        route = route or f"rt-{n:016x}"
        directory = f"campaigns/2026-10-01_c/2026-10-01_cycle-{n}"
        self.write(f"{directory}/.cycle.json", json.dumps({"cycle_id": cycle_id, "kind": "artifact-cycle-binding"}).encode())
        revisions = []
        for index, (rel, data) in enumerate(files.items()):
            self.write(f"{directory}/{rel}", data)
            revisions.append({"artifact_id": f"art_{n}_{index}", "revision_sequence": 1,
                              "locator": {"kind": "cycle-relative", "path": rel},
                              "content_digest": (digests or {}).get(rel, sha(data)), "byte_size": len(data)})
        self.write(f"{directory}/manifest.json", json.dumps({"artifact_revisions": revisions}).encode())
        self.write(f".runtime/artifact-producer/v1/cycles/{cycle_id}.json", json.dumps(
            {"cycle_id": cycle_id, "campaign_id": "camp_x", "state": state, "route_id": route}).encode())
        if closed:
            self.write(f".runtime/routes/{route}.outcome.json", b"{}")
        return directory

    def snapshot(self):
        out = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                st = path.lstat()
                out[path.relative_to(self.root).as_posix()] = (st.st_ino, st.st_mtime_ns, st.st_mode, path.read_bytes())
        return out


HIST1, HIST2 = b"history one\n" * 50, b"history two\n" * 50


def standard(base):
    """Two revisions sharing a history row, and one sealed cycle seeded from r2."""
    s = Store(base)
    s.revision("rrev_1", {"prd.md": b"live 1\n", "_internal/versions/v1/prd.md": HIST1})
    s.revision("rrev_2", {"prd.md": b"live 2\n", "_internal/versions/v1/prd.md": HIST1,
                          "_internal/versions/v2/prd.md": HIST2})
    s.cycle(1, {"artifacts/spec/prd.md": b"live 2\n",
                "artifacts/spec/_internal/versions/v1/prd.md": HIST1,
                "artifacts/spec/_internal/versions/v2/prd.md": HIST2,
                "artifacts/plans/report.md": b"same\n" * 2000})
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # Fixture files were created just now (fresh ctime); plan as if two days later.
        self.now_ns = time.time_ns() + 2 * D.RECENT_NS

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, store, name="j.jsonl", **kw):
        journal = self.base / name
        kw.setdefault("now_ns", self.now_ns)
        header, groups, summary = D.plan(store.root, journal, jobs=None, max_seconds=60, **kw)
        return journal, header, groups, summary

    def linked_paths(self, store, groups):
        """Planned paths that share their group's plan-time primary inode right now."""
        return [link["path"] for g in groups for link in g["links"]
                if os.lstat(store.root / link["path"]).st_ino == g["primary"]["ino"]]


class PlanTest(Base):
    def test_dry_run_changes_nothing_and_counts_only_immutable_classes(self):
        s = standard(self.base)
        before = s.snapshot()
        journal, header, groups, summary = self.plan(s)
        self.assertEqual(s.snapshot(), before)
        linked = sorted(link["path"] for g in groups for link in g["links"])
        cycle = "campaigns/2026-10-01_c/2026-10-01_cycle-1/artifacts/spec/_internal/versions"
        self.assertEqual(linked, [f"{cycle}/v1/prd.md", f"{cycle}/v2/prd.md",
                                  f"shared/spec/{REF}/revisions/rrev_2/_internal/versions/v1/prd.md"])
        primaries = {g["primary"]["path"] for g in groups}
        self.assertTrue(all(p.startswith("shared/spec/") for p in primaries))
        self.assertEqual(summary["freed_bytes"], 2 * len(HIST1) + len(HIST2))
        self.assertIn("live-cycle-file", summary["skipped"])  # live prd.md and report.md
        self.assertEqual(summary["skipped"]["live-cycle-file"]["files"], 2)
        self.assertTrue(journal.is_file())
        self.assertEqual(D.Journal(journal).summary["groups"], len(groups))

    def test_open_unfinished_and_registry_cycles_are_excluded(self):
        s = standard(self.base)
        s.cycle(2, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1}, state="open")
        s.cycle(3, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1}, closed=False)
        s.cycle(4, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1}, route="rt-busy")
        s.cycle(5, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1}, state="abandoned")
        with mock.patch.object(D, "open_registry_routes", return_value=({"rt-busy"}, "fixture")):
            _, _, groups, summary = self.plan(s)
        self.assertEqual(summary["cycles"]["excluded_by_reason"],
                         {"cycle-not-sealed": 1, "cycle-open": 1, "route-open-attempt": 1, "route-unfinished": 1})
        linked = {link["path"] for g in groups for link in g["links"]}
        self.assertFalse(any("cycle-2" in p or "cycle-3" in p or "cycle-4" in p or "cycle-5" in p for p in linked))

    def test_recent_symlink_external_link_and_restore_target_are_skipped(self):
        s = standard(self.base)
        d6 = s.cycle(6, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1})
        recent = self.now_ns - 3600 * 10**9  # an hour before the plan
        os.utime(s.root / d6 / "artifacts/spec/_internal/versions/v1/prd.md", ns=(recent, recent))
        d7 = s.cycle(7, {"artifacts/spec/_internal/versions/v2/prd.md": HIST2})
        target = s.root / d7 / "artifacts/spec/_internal/versions/v2/prd.md"
        os.link(target, self.base / "outside-link")
        d8 = s.cycle(8, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1})
        p8 = s.root / d8 / "artifacts/spec/_internal/versions/v1/prd.md"
        p8.unlink(); os.symlink(s.root / f"shared/spec/{REF}/revisions/rrev_1/_internal/versions/v1/prd.md", p8)
        d9 = s.cycle(9, {"artifacts/spec/_internal/versions/v2/prd.md": HIST2})
        s.write("campaigns/2026-10-01_c/2026-10-01_cycle-1/artifacts/plans/sealed-bytes-restore/_internal/requests/x/request.json",
                json.dumps({"target": cyc(9)}).encode())
        _, header, groups, summary = self.plan(s)
        skipped = summary["skipped"]
        self.assertEqual(skipped["recent-24h"]["files"], 1)
        self.assertEqual(skipped["external-hardlink"]["files"], 1)
        self.assertEqual(skipped["symlink"]["files"], 1)
        self.assertEqual(summary["cycles"]["excluded_by_reason"].get("restore-target"), 1)
        self.assertIn(cyc(9), header["restore_target_cycles"])
        linked = {link["path"] for g in groups for link in g["links"]}
        for d in (d6, d7, d8, d9):
            self.assertFalse(any(p.startswith(d + "/") for p in linked))

    def test_symlinked_parent_directory_is_skipped(self):
        s = standard(self.base)
        real = self.base / "elsewhere"
        (real / "spec/_internal/versions/v1").mkdir(parents=True)
        (real / "spec/_internal/versions/v1/prd.md").write_bytes(HIST1)
        os.utime(real / "spec/_internal/versions/v1/prd.md", (OLD, OLD))
        d = s.cycle(10, {})
        os.symlink(real, s.root / d / "artifacts")
        manifest = {"artifact_revisions": [{"artifact_id": "a", "revision_sequence": 1, "byte_size": len(HIST1),
                    "content_digest": sha(HIST1), "locator": {"kind": "cycle-relative",
                    "path": "artifacts/spec/_internal/versions/v1/prd.md"}}]}
        (s.root / d / "manifest.json").write_text(json.dumps(manifest))
        _, _, _, summary = self.plan(s)
        self.assertEqual(summary["skipped"]["symlinked-parent"]["files"], 1)

    def test_other_volume_owner_and_mode_split_groups(self):
        s = standard(self.base)
        odd = str(s.root / "campaigns/2026-10-01_c/2026-10-01_cycle-1/artifacts/spec/_internal/versions/v2/prd.md")
        real = os.lstat

        def fake(path, *a, **k):
            st = real(path, *a, **k)
            if str(path) != odd:
                return st
            other = type("Stat", (), {n: getattr(st, n) for n in dir(st) if n.startswith("st_")})()
            other.st_dev = st.st_dev + 1
            return other
        with mock.patch.object(D, "_lstat", fake):
            _, _, groups, summary = self.plan(s)
        self.assertEqual(summary["skipped"]["other-volume"]["files"], 1)
        self.assertNotIn(odd, {str(s.root / link["path"]) for g in groups for link in g["links"]})
        (self.base / "m").mkdir()
        s2 = standard(self.base / "m")
        os.chmod(s2.root / "campaigns/2026-10-01_c/2026-10-01_cycle-1/artifacts/spec/_internal/versions/v2/prd.md", 0o600)
        _, _, _, summary2 = self.plan(s2, name="m.jsonl")
        self.assertEqual(summary2["skipped"]["mode-differs"]["files"], 1)

    def test_journal_is_never_overwritten_and_deadline_writes_nothing(self):
        s = standard(self.base)
        journal, *_ = self.plan(s)
        with self.assertRaises(D.DedupError) as caught:
            D.plan(s.root, journal, jobs=None, max_seconds=60)
        self.assertEqual(caught.exception.reason, "journal-exists")
        with mock.patch.object(D, "_Deadline", lambda seconds: type("X", (), {
                "check": lambda self, phase: (_ for _ in ()).throw(D.DedupError("deadline-exceeded", phase, code=75)),
                "expired": lambda self: True})()):
            with self.assertRaises(D.DedupError):
                D.plan(s.root, self.base / "late.jsonl", jobs=None, max_seconds=1)
        self.assertFalse((self.base / "late.jsonl").exists())

    def test_cli_requires_an_explicit_command_and_registry_choice(self):
        s = standard(self.base)
        run = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        env = {**os.environ, "AGENT_DISPATCH_JOBS": str(self.base / "absent.log")}
        run = subprocess.run([sys.executable, str(TOOL), "plan", "--root", str(s.root)], capture_output=True,
                             text=True, env=env)
        self.assertEqual(run.returncode, 66, run.stderr)
        self.assertIn("registry-unavailable", run.stderr)
        run = subprocess.run([sys.executable, str(TOOL), "plan", "--root", str(s.root), "--jobs", "none"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        # The fixture was written just now, so every candidate is inside the 24 h window.
        self.assertEqual((out["link_files"], out["skipped"]["recent-24h"]["files"]), (0, 7))

    def test_recent_ctime_counts_even_with_an_old_mtime(self):
        # `cp -p` / copy2: an old mtime on a file that just arrived; its ctime is now.
        s = standard(self.base)
        now = time.time_ns()
        _, _, groups, summary = self.plan(s, now_ns=now)
        self.assertEqual(groups, [])
        self.assertEqual(summary["skipped"]["recent-24h"]["files"], 7)
        _, _, groups, _ = self.plan(s, name="later.jsonl", now_ns=now + D.RECENT_NS + 60 * 10**9)
        self.assertEqual(sum(len(g["links"]) for g in groups), 3)

    def test_manifest_locator_leaving_its_cycle_is_skipped(self):
        s = standard(self.base)
        open_dir = s.cycle(2, {"artifacts/spec/_internal/versions/v1/prd.md": HIST1}, state="open")
        target = s.root / open_dir / "artifacts/spec/_internal/versions/v1/prd.md"
        sealed = s.cycle(3, {})
        rows = [{"artifact_id": f"x{i}", "revision_sequence": 1, "content_digest": sha(HIST1), "byte_size": len(HIST1),
                 "locator": {"kind": "cycle-relative", "path": rel}}
                for i, rel in enumerate([f"artifacts/spec/../../2026-10-01_cycle-2/artifacts/spec/_internal/versions/v1/prd.md",
                                         str(target)])]
        (s.root / sealed / "manifest.json").write_text(json.dumps({"artifact_revisions": rows}))
        journal, _, groups, summary = self.plan(s)
        self.assertEqual(summary["skipped"]["locator-outside-cycle"]["files"], 2)
        self.assertFalse(any("cycle-2" in link["path"] or "/../" in link["path"] for g in groups for link in g["links"]))
        D.apply(journal)
        self.assertEqual(os.stat(target).st_nlink, 1)


class ApplyTest(Base):
    def applied(self):
        s = standard(self.base)
        original = s.snapshot()
        journal, _, groups, _ = self.plan(s)
        totals = D.apply(journal)
        return s, original, journal, groups, totals

    def test_apply_links_verify_and_revert_restore_everything(self):
        s, original, journal, groups, totals = self.applied()
        self.assertEqual(totals["linked"], 3)
        self.assertEqual(totals["freed_bytes"], 2 * len(HIST1) + len(HIST2))
        for g in groups:
            for link in g["links"]:
                self.assertTrue(os.path.samefile(s.root / link["path"], s.root / g["primary"]["path"]))
        after = s.snapshot()
        self.assertEqual({k: v[3] for k, v in after.items() if k in original},
                         {k: v[3] for k, v in original.items()})
        self.assertFalse((s.root / ".runtime/dedup-hardlink").exists() and
                         any((s.root / ".runtime/dedup-hardlink").rglob("*.tmp")))
        report = D.verify(journal)
        self.assertEqual((report["linked_ok"], report["diverged"], report["primary_bad"]), (3, 0, 0))
        again = D.apply(journal)  # nothing left to do
        self.assertEqual(again["groups_done"], 0)
        result = D.revert(journal)
        self.assertEqual((result["restored"], result["remaining_groups"]), (3, 0))
        restored = s.snapshot()
        for rel, (ino, mtime_ns, mode, data) in original.items():
            if rel.startswith(".runtime/dedup-hardlink"):
                continue
            self.assertEqual(restored[rel][1:], (mtime_ns, mode, data), rel)
        for g in groups:
            for link in g["links"]:
                self.assertFalse(os.path.samefile(s.root / link["path"], s.root / g["primary"]["path"]))
        self.assertEqual(D.verify(journal)["reverted_groups"], len(groups))

    def test_atomic_writer_breaks_only_its_own_link(self):
        s, original, journal, groups, _ = self.applied()
        target = s.root / "campaigns/2026-10-01_c/2026-10-01_cycle-1/artifacts/spec/_internal/versions/v1/prd.md"
        sys.path.insert(0, str(ROOT / "utilities"))
        import artifact_producer_store as store
        store._write_atomic(target, b"rewritten\n")
        for rel in (f"shared/spec/{REF}/revisions/rrev_1/_internal/versions/v1/prd.md",
                    f"shared/spec/{REF}/revisions/rrev_2/_internal/versions/v1/prd.md"):
            self.assertEqual((s.root / rel).read_bytes(), HIST1)
        report = D.verify(journal)
        self.assertEqual((report["diverged"], report["primary_bad"]), (1, 0))
        result = D.revert(journal)
        self.assertEqual(result["skipped"], {"not-linked-now": 1})
        self.assertEqual(target.read_bytes(), b"rewritten\n")

    def test_same_size_different_bytes_with_a_lying_manifest_is_not_linked(self):
        s = Store(self.base)
        s.revision("rrev_1", {"_internal/versions/v1/prd.md": b"A" * 4096})
        fake = b"B" * 4096  # same size; the manifest claims the revision's digest
        d = s.cycle(1, {"artifacts/spec/_internal/versions/v1/prd.md": fake},
                    digests={"artifacts/spec/_internal/versions/v1/prd.md": sha(b"A" * 4096)})
        journal, _, groups, _ = self.plan(s)
        self.assertEqual(len(groups), 1)
        totals = D.apply(journal)
        self.assertEqual(totals["linked"], 0)
        self.assertEqual(totals["skipped"], {"content-differs": 1})
        self.assertEqual((s.root / d / "artifacts/spec/_internal/versions/v1/prd.md").read_bytes(), fake)

    def test_primary_whose_bytes_differ_from_its_digest_skips_the_group(self):
        s = standard(self.base)
        journal, _, groups, _ = self.plan(s)
        primary = s.root / groups[0]["primary"]["path"]
        st = primary.lstat()
        with open(primary, "r+b") as handle:  # same size, same mtime: corrupt in place
            handle.write(b"X")
        os.utime(primary, ns=(st.st_atime_ns, st.st_mtime_ns))
        totals = D.apply(journal)
        self.assertEqual(totals["group_skipped"], {"primary-digest-mismatch": 1})

    def test_file_changed_after_plan_is_skipped(self):
        s = standard(self.base)
        journal, _, groups, _ = self.plan(s)
        dup = s.root / groups[0]["links"][0]["path"]
        dup.write_bytes(dup.read_bytes())  # same bytes, new mtime
        totals = D.apply(journal)
        self.assertEqual(totals["skipped"].get("changed-since-plan"), 1)
        self.assertFalse(os.path.samefile(dup, s.root / groups[0]["primary"]["path"]))

    def test_file_changed_during_apply_is_skipped_and_keeps_new_bytes(self):
        s = standard(self.base)
        journal, _, groups, _ = self.plan(s)
        victim = str(s.root / groups[0]["links"][0]["path"])

        def write_now(path):
            if path == victim:
                with open(path, "ab") as handle:  # an in-place writer racing the replace
                    handle.write(b"late\n")
        with mock.patch.object(D, "_before_replace", write_now):
            totals = D.apply(journal)
        self.assertEqual(totals["skipped"].get("changed-during-apply"), 1)
        self.assertTrue(Path(victim).read_bytes().endswith(b"late\n"))
        self.assertEqual(list((s.root / ".runtime/dedup-hardlink").rglob("*.tmp")), [])

    def test_sigkill_mid_apply_resumes_and_finishes(self):
        s = Store(self.base)
        rows = {f"_internal/versions/v{i}/prd.md": (b"%d\n" % i) * 500 for i in range(1, 7)}
        s.revision("rrev_1", rows)
        s.revision("rrev_2", rows)
        s.cycle(1, {"artifacts/spec/" + k: v for k, v in rows.items()})
        journal, _, groups, summary = self.plan(s)
        self.assertEqual(summary["link_files"], 12)
        wrapper = self.base / "killer.py"
        wrapper.write_text(
            "import importlib.util, os, signal, sys\n"
            f"spec = importlib.util.spec_from_file_location('t', {str(TOOL)!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "real, n = os.replace, [0]\n"
            "def replace(a, b):\n"
            "    real(a, b); n[0] += 1\n"
            "    if n[0] == 5: os.kill(os.getpid(), signal.SIGKILL)\n"
            "m.os.replace = replace\n"
            "sys.exit(m.main(['apply', sys.argv[1]]))\n")
        run = subprocess.run([sys.executable, str(wrapper), str(journal)], capture_output=True)
        self.assertEqual(run.returncode, -9)
        partial = D.Journal(journal)
        self.assertLess(len(partial.applied), len(groups))
        totals = D.apply(journal)
        self.assertGreaterEqual(totals["already"], 1)
        self.assertEqual(D.verify(journal)["linked_ok"], 12)
        self.assertEqual(list((s.root / ".runtime/dedup-hardlink").rglob("*.tmp")), [])
        D.revert(journal)
        for g in groups:
            for link in g["links"]:
                self.assertEqual(os.stat(s.root / link["path"]).st_nlink, 1)

    def test_torn_journal_tail_is_tolerated(self):
        s = standard(self.base)
        journal, _, groups, _ = self.plan(s)
        with open(journal, "a") as handle:
            handle.write('{"kind": "applied", "gro')
        totals = D.apply(journal)
        self.assertEqual(totals["linked"], 3)
        self.assertEqual(D.Journal(journal).torn, 1)
        self.assertEqual(D.verify(journal)["linked_ok"], 3)

    def test_staging_on_another_volume_links_nothing(self):
        shm = Path("/dev/shm")
        if not shm.is_dir() or os.stat(shm).st_dev == os.stat(self.base).st_dev:
            self.skipTest("no second volume")
        s = standard(self.base)
        journal, _, groups, _ = self.plan(s)
        with tempfile.TemporaryDirectory(dir=shm) as other:
            totals = D.apply(journal, staging=other)
        self.assertEqual(totals["linked"], 0)
        self.assertEqual(totals["group_skipped"], {"staging-other-volume": len(groups)})
        self.assertEqual(totals["remaining_groups"], len(groups))
        retried = D.apply(journal)  # an environment skip is retried with a same-volume staging
        self.assertEqual(retried["linked"], 3)
        self.assertEqual(D.verify(journal)["linked_ok"], 3)

    def test_one_unreadable_path_is_skipped_not_fatal(self):
        import errno
        s = standard(self.base)
        journal, _, groups, _ = self.plan(s)
        pair = next(g for g in groups if len(g["links"]) == 2)  # HIST1: one link left, plus HIST2's
        victim = str(s.root / pair["links"][0]["path"])
        real = D._open_regular

        def denied(path):
            if path == victim:
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real(path)
        with mock.patch.object(D, "_open_regular", denied):
            totals = D.apply(journal)
        self.assertEqual(totals["skipped"], {"os-error-EACCES": 1})
        self.assertEqual(totals["linked"], 2)
        D.apply(journal)
        with mock.patch.object(D, "_open_regular", lambda path: (_ for _ in ()).throw(
                OSError(errno.ESTALE, "Stale file handle", path))):
            first = D.revert(journal)
        self.assertEqual(first["skipped"], {"os-error-ESTALE": 2})
        self.assertEqual((first["remaining_groups"], first["still_linked"]), (2, 2))
        second = D.revert(journal)
        self.assertEqual((second["restored"], second["remaining_groups"], second["still_linked"]), (2, 0, 0))
        self.assertEqual(self.linked_paths(s, groups), [])

    def test_concurrent_apply_is_refused(self):
        import fcntl
        s = standard(self.base)
        journal, *_ = self.plan(s)
        fd = os.open(journal, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(D.DedupError) as caught:
                D.apply(journal)
            self.assertEqual(caught.exception.reason, "journal-busy")
        finally:
            os.close(fd)


class RevertTest(Base):
    def test_revert_after_a_group_was_cut_short_restores_unjournaled_links(self):
        # An apply killed after some `os.replace` of a group, before its `applied` line.
        s = Store(self.base)
        rows = {"a.md": b"a" * 5000, "b.md": b"b" * 5000}
        for rid in ("rrev_1", "rrev_2", "rrev_3"):
            s.revision(rid, rows)
        original = s.snapshot()
        journal, _, groups, _ = self.plan(s)
        self.assertEqual([len(g["links"]) for g in groups], [2, 2])
        wrapper = self.base / "killer.py"
        wrapper.write_text(
            "import importlib.util, os, signal, sys\n"
            f"spec = importlib.util.spec_from_file_location('t', {str(TOOL)!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "real, n = os.replace, [0]\n"
            "def replace(a, b):\n"
            "    real(a, b); n[0] += 1\n"
            "    if n[0] == 3: os.kill(os.getpid(), signal.SIGKILL)\n"
            "m.os.replace = replace\n"
            "sys.exit(m.main(['apply', sys.argv[1]]))\n")
        run = subprocess.run([sys.executable, str(wrapper), str(journal)], capture_output=True)
        self.assertEqual(run.returncode, -9)
        self.assertEqual(len(D.Journal(journal).applied), 1)  # the 2nd group linked one path, unjournaled
        self.assertEqual(len(self.linked_paths(s, groups)), 3)
        report = D.verify(journal)
        self.assertEqual((report["still_linked"], report["unrecorded_links"]), (3, 1))
        result = D.revert(journal)  # straight away, without re-running apply
        self.assertEqual((result["restored"], result["remaining_groups"], result["still_linked"]), (3, 0, 0))
        self.assertEqual(self.linked_paths(s, groups), [])
        restored = s.snapshot()
        for rel, (_, mtime_ns, mode, data) in original.items():
            self.assertEqual(restored[rel][1:], (mtime_ns, mode, data), rel)
        report = D.verify(journal)
        self.assertEqual((report["still_linked"], report["reverted_groups"]), (0, 2))
        self.assertEqual(D.apply(journal)["groups_done"], 0)  # a reverted journal links nothing more
        self.assertFalse((s.root / ".runtime/dedup-hardlink").exists())

    def test_a_path_skipped_during_revert_is_retried_and_counted(self):
        # 2nd/3rd lstat of the path in a revert: `_revert_path`'s first check, and the
        # re-check just before its `os.replace`. A passing glitch at either keeps the group open.
        for call in (2, 3):
            with self.subTest(call=call):
                (self.base / f"call{call}").mkdir()
                s = Store(self.base / f"call{call}")
                for rid in ("rrev_1", "rrev_2", "rrev_3"):
                    s.revision(rid, {"a.md": b"z" * 4000})
                journal, _, groups, _ = self.plan(s, name=f"call{call}.jsonl")
                D.apply(journal)
                victim = str(s.root / groups[0]["links"][-1]["path"])
                real, calls = D._lstat, {"n": 0}

                def flaky(path):
                    st = real(path)
                    if path == victim:
                        calls["n"] += 1
                        if calls["n"] == call:
                            return type("Stat", (), {"st_ino": -1, "st_mode": st.st_mode})()
                    return st
                with mock.patch.object(D, "_lstat", flaky):
                    first = D.revert(journal)
                self.assertEqual(first["skipped"], {"changed-during-revert": 1})
                self.assertEqual((first["restored"], first["remaining_groups"], first["still_linked"]), (1, 1, 1))
                report = D.verify(journal)
                self.assertEqual((report["still_linked"], report["reverted_groups"]), (1, 0))
                second = D.revert(journal)
                self.assertEqual((second["groups_done"], second["restored"], second["skipped"]), (1, 1, {}))
                self.assertEqual((second["remaining_groups"], second["still_linked"]), (0, 0))
                self.assertEqual(self.linked_paths(s, groups), [])
                report = D.verify(journal)
                self.assertEqual((report["still_linked"], report["reverted_groups"]), (0, 1))
                self.assertEqual(D.revert(journal)["groups_done"], 0)

    def test_verify_and_revert_find_links_after_the_volume_is_renumbered(self):
        # A reboot or remount gives an NFS volume a new st_dev; inode numbers stay. Rewriting
        # the journal's recorded numbers leaves the tool seeing exactly that state.
        s = Store(self.base)
        for rid in ("rrev_1", "rrev_2", "rrev_3"):
            s.revision(rid, {"a.md": b"a" * 5000, "b.md": b"b" * 5000})
        original = s.snapshot()
        journal, header, groups, _ = self.plan(s)
        self.assertEqual(D.apply(journal)["linked"], 4)
        old, new = header["root_dev"], header["root_dev"] + 1

        def renumber(value):
            if isinstance(value, dict):
                return {k: new if k in ("dev", "root_dev") and v == old else renumber(v) for k, v in value.items()}
            return [renumber(v) for v in value] if isinstance(value, list) else value
        lines = [renumber(json.loads(line)) for line in journal.read_text().splitlines()]
        journal.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in lines))
        self.assertEqual(D.Journal(journal).groups[0]["primary"]["dev"], new)
        report = D.verify(journal)
        self.assertEqual((report["linked_ok"], report["still_linked"], report["diverged"]), (4, 4, 0))
        result = D.revert(journal)
        self.assertEqual((result["restored"], result["remaining_groups"], result["still_linked"]), (4, 0, 0))
        self.assertEqual(result["skipped"], {})
        self.assertEqual(self.linked_paths(s, groups), [])
        restored = s.snapshot()
        for rel, (_, mtime_ns, mode, data) in original.items():
            self.assertEqual(restored[rel][1:], (mtime_ns, mode, data), rel)
        report = D.verify(journal)
        self.assertEqual((report["still_linked"], report["reverted_groups"]), (0, 2))

if __name__ == "__main__":
    unittest.main()
