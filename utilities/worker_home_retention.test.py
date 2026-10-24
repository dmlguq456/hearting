"""Automatic retention of expired per-attempt worker homes (issue #564).

Every fixture lives in a temporary dispatch state root; nothing reads or
touches a live registry or home.
"""
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_contract as DC  # noqa: E402
import dispatch_attempt_reference as AR  # noqa: E402
import worker_home_retention as R  # noqa: E402
import dispatch_pending_delivery as P  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import session_notice as NOTICE  # noqa: E402
from route_identity import route_hash  # noqa: E402
from worker_runtime_home import _builder  # noqa: E402

RETENTION_NS = R.IDLE_SECONDS * 10**9
SOURCE = str(ROOT)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.old_title_state = os.environ.get("FLEET_TITLE_STATE_DIR")
        os.environ["FLEET_TITLE_STATE_DIR"] = str(self.root / "fleet-titles")
        self.addCleanup(self._restore_title_state)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.workers = self.root / "homes" / "workers"
        self.workers.mkdir(parents=True, mode=0o700)
        self.outside = self.root.parent / (self.root.name + "-outside")
        self.outside.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.outside, ignore_errors=True))
        self.rows = {}
        self.timestamps = {}
        self.now = time.time_ns()

    def _restore_title_state(self):
        if self.old_title_state is None:
            os.environ.pop("FLEET_TITLE_STATE_DIR", None)
        else:
            os.environ["FLEET_TITLE_STATE_DIR"] = self.old_title_state

    # --- builders --------------------------------------------------------
    def key(self, attempt, harness="claude", worker_type="stage"):
        return _builder.worker_home_key(SOURCE, harness, worker_type, attempt)

    def home_path(self, attempt, **kwargs):
        return self.workers / self.key(attempt, **kwargs)

    def write_rows(self):
        lines = []
        for attempt, (status, meta) in self.rows.items():
            pipe = ",".join(f"{k}={v}" for k, v in meta.items())
            timestamp = self.timestamps.get(attempt)
            if timestamp is None:
                timestamp = datetime.fromtimestamp(self.now / 10**9, timezone.utc).isoformat().replace("+00:00", "Z")
            lines.append(f"{timestamp}\t{status}\t/repo\t/wt\t{attempt}\t{pipe}")
        self.jobs.write_text("\n".join(lines) + ("\n" if lines else ""))

    def set_timestamp(self, attempt, timestamp):
        self.timestamps[attempt] = timestamp
        self.write_rows()

    def row(self, attempt, status="done", **meta):
        base = {"attempt_id": attempt, "harness": "claude", "worker_type": "stage",
                "launch_outcome": "never-launched", "main_runtime_harness": "claude",
                "main_runtime_pid": "2147483647", "main_runtime_pid_start": "1",
                "main_runtime_ns": DC.process_namespace_identity()}
        base.update(meta)
        self.rows[attempt] = (status, base)
        self.write_rows()

    def append_row(self, attempt, status="done", **meta):
        base = {"attempt_id": attempt, "harness": "claude", "worker_type": "stage",
                "launch_outcome": "never-launched", "main_runtime_harness": "claude",
                "main_runtime_pid": "2147483647", "main_runtime_pid_start": "1",
                "main_runtime_ns": DC.process_namespace_identity()}
        base.update(meta)
        self.rows[attempt] = (status, base)
        pipe = ",".join(f"{key}={value}" for key, value in base.items())
        timestamp = datetime.fromtimestamp(
            self.now / 10**9, timezone.utc
        ).isoformat().replace("+00:00", "Z")
        with self.jobs.open("ab") as handle:
            handle.write(f"{timestamp}\t{status}\t/repo\t/wt\t{attempt}\t{pipe}\n".encode())
            handle.flush()

    def set_status(self, attempt, status):
        self.rows[attempt] = (status, self.rows[attempt][1])
        self.write_rows()

    def add(self, attempt, status="done", *, declare=True, schema=2, home=True, destination=False,
            harness="claude", worker_type="stage", **meta):
        key = self.key(attempt, harness, worker_type)
        path = self.workers / key
        if declare:
            meta.setdefault("runtime_home", str(path))
        self.row(attempt, status, harness=harness, worker_type=worker_type, **meta)
        if home:
            path.mkdir(mode=0o700)
            manifest = {"harness": harness, "worker_type": worker_type, "source": SOURCE, "profile": None}
            if schema == 2:
                manifest.update(schema=2, identity=attempt, key=key, destination=destination)
            (path / "worker-home.json").write_text(json.dumps(manifest))
            (path / "projects").mkdir()
            (path / "projects" / "transcript.jsonl").write_text("x" * 100)
        return path

    def add_unmapped_legacy(self, attempt, *, harness="claude", worker_type="stage"):
        """Build a valid schema-1 HOME with no row or retained attempt ledger."""
        path = self.add(attempt, schema=1, harness=harness, worker_type=worker_type)
        self.rows.pop(attempt, None)
        self.timestamps.pop(attempt, None)
        self.write_rows()
        return path

    def attach_summary_owner(self, attempt, home, harness="claude"):
        import dispatch_summary as summary

        identity = DC.process_launch_identity(os.getpid())
        namespace = DC.process_namespace_identity()
        state_path = summary.owner_state_path(harness, attempt)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        transcript = home / "projects" / "transcript.jsonl"
        metadata = {
            "summary_owner": summary.OWNER_KIND,
            "summary_sid": summary.summary_sid(attempt),
            "summary_owner_pid": str(os.getpid()),
            "summary_owner_pid_start": identity["pid_start"],
            "summary_owner_pid_observer_ns": namespace,
            "summary_state_file": str(state_path),
        }
        self.rows[attempt][1].update(metadata)
        self.write_rows()
        state_path.write_text(json.dumps({
            "schema_version": summary.OWNER_SCHEMA,
            "status": "active",
            "attempt_id": attempt,
            "harness": harness,
            "sid": summary.summary_sid(attempt),
            "transcript": str(transcript),
            "runtime_home": str(home),
            "pid": os.getpid(),
            "proc_start": identity["pid_start"],
            "observer_namespace": namespace,
        }))
        owner_lock = state_path.with_suffix(".lock")
        owner_lock.touch()
        provider_lock = Path(os.environ["FLEET_TITLE_STATE_DIR"]) / harness / \
            (".lock-" + summary.summary_sid(attempt))
        return owner_lock, provider_lock

    def sweep(self, windows=0.0, **kwargs):
        return R.sweep(self.jobs, now_ns=self.now + int(windows * RETENTION_NS), force=True, **kwargs)

    def expire(self, windows=8 / 7):
        self.sweep(0)
        return self.sweep(windows)

    def state(self):
        return json.loads((self.root / "homes/worker-retention/state.json").read_text())

    def delivery_record(self, path, attempt, parent="att-parent", state="acked", **changes):
        value = dict.fromkeys(P.REQUIRED_FIELDS)
        value.update(schema_version=P.SCHEMA_VERSION, delivery_id=path.name[:-5],
                     recipient_kind=next(iter(P.RECIPIENT_KINDS)),
                     recipient_digest=path.parent.name, attempt_ids=[attempt],
                     parent_attempt_id=parent, state=state, attempts=0,
                     claim_authority=next(iter(P.CLAIM_AUTHORITIES)))
        value.update(changes)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return value

    def publish_delivery(self, delivery_id, recipient_key, attempt_ids, parent_attempt_id):
        receipt = {"schema_version": 2, "fixture": "reference-admission"}
        return P.create(
            self.root, recipient_kind="codex-native-queue", recipient_key=recipient_key,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=list(attempt_ids), parent_attempt_id=parent_attempt_id,
            route_id="rt-reference-admission", route_node="execute", receipt=receipt,
            receipt_digest=P._canonical_receipt_digest(receipt), row_revisions={},
        )

    def kept(self, result, reason):
        return result["kept"].get(reason, 0)

    def unknown(self, result, reason):
        return result["unknown"].get(reason, 0)

    @contextlib.contextmanager
    def replaced(self, name, fake):
        """Swap an os function; the support tables the module consults follow it."""
        with mock.patch.object(os, name, fake), \
                mock.patch.object(os, "supports_dir_fd", set(os.supports_dir_fd) | {fake}), \
                mock.patch.object(os, "supports_follow_symlinks", set(os.supports_follow_symlinks) | {fake}), \
                mock.patch.object(os, "supports_fd", set(os.supports_fd) | {fake}):
            yield

    @contextlib.contextmanager
    def process_inventory(self, *pids, unreadable_environ=()):
        """Bound /proc enumeration while retaining real observations for listed PIDs."""
        proc_root = Path("/proc")
        entries = tuple(proc_root / str(pid) for pid in pids)
        unreadable = {proc_root / str(pid) / "environ" for pid in unreadable_environ}
        original_iterdir, original_read_bytes = Path.iterdir, Path.read_bytes

        def iterdir(path):
            if path == proc_root:
                return iter(entries)
            return original_iterdir(path)

        def read_bytes(path):
            if path in unreadable:
                raise PermissionError("fixture process environment is unreadable")
            return original_read_bytes(path)

        with mock.patch.object(Path, "iterdir", iterdir), \
                mock.patch.object(Path, "read_bytes", read_bytes):
            yield

    @contextlib.contextmanager
    def landing(self, mutate, *, key=None):
        """Run `mutate` once, just before the registry lock of a detach is taken.

        With `key`, only the detach of that home triggers it: home keys hash the
        checkout path, so the order of several due homes differs per checkout.
        """
        original, original_detach = R._registry_lock, R._detach
        fired, detaching = [], []

        def detach(ctx, home_key, *args, **kwargs):
            detaching[:] = [home_key]
            return original_detach(ctx, home_key, *args, **kwargs)

        @contextlib.contextmanager
        def lock(jobs):
            if not fired and (key is None or detaching == [key]):
                fired.append(True)
                mutate()
            with original(jobs):
                yield

        with mock.patch.object(R, "_registry_lock", lock), mock.patch.object(R, "_detach", detach):
            yield fired


class Expiry(Fixture):
    def test_terminal_claude_prunes_cache_but_resolves_transcript(self):
        from claude_project_dir import encode_project_dir, resolve_session_transcript
        attempt = "att-live-main-retention"
        home = self.add(attempt)
        identity = DC.process_launch_identity(os.getpid())
        self.rows[attempt][1].update(
            main_runtime_harness="claude", main_runtime_pid=str(os.getpid()),
            main_runtime_pid_start=identity["pid_start"], main_runtime_ns=DC.process_namespace_identity())
        self.write_rows()
        transcript = home / "projects" / encode_project_dir("/repo") / "session-raw.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"message"}\n')
        raw_inode = transcript.stat().st_ino
        cache = home / "cache"
        cache.mkdir()
        (cache / "large.bin").write_bytes(b"cache" * 1024)
        os.utime(home, ns=(self.now, self.now))
        with mock.patch.object(R._Context, "home_process_evidence",
                               return_value=("absent", "fixture-process-scan-complete")):
            result = self.sweep(1.01)
        self.assertEqual(result["partial"], 1, result)
        self.assertTrue(home.is_dir())
        self.assertTrue((home / "worker-home.json").is_file())
        self.assertTrue((home / "projects").is_dir())
        self.assertFalse(cache.exists())
        self.assertEqual(transcript.stat().st_ino, raw_inode)
        resolved, reason = resolve_session_transcript(
            "session-raw", "/repo", expected_cwd="/repo", runtime_home=str(home))
        self.assertIsNone(reason)
        self.assertEqual(resolved, transcript)
        with R.worker_home_read_admission(
                self.jobs, home, transcript, attempt, "claude") as admitted:
            self.assertTrue(admitted)
        rebuild_env = {"HOME": str(self.root / "runtime-user"),
                       "XDG_CONFIG_HOME": str(self.root / "runtime-config"),
                       "XDG_DATA_HOME": str(self.root / "runtime-data"),
                       "XDG_CACHE_HOME": str(self.root / "runtime-cache"),
                       "AGENT_DISPATCH_JOBS": str(self.jobs)}
        _builder.build_worker_home(SOURCE, "claude", "stage", attempt,
                                   env=rebuild_env, destination=home)
        self.assertEqual(transcript.stat().st_ino, raw_inode)
        self.assertEqual(transcript.read_text(), '{"type":"message"}\n')

    def test_terminal_opencode_prunes_cache_preserving_database_wal_and_shm(self):
        import sqlite3
        attempt = "att-opencode-live-main-retention"
        home = self.add(attempt, harness="opencode")
        identity = DC.process_launch_identity(os.getpid())
        self.rows[attempt][1].update(
            main_runtime_harness="opencode", main_runtime_pid=str(os.getpid()),
            main_runtime_pid_start=identity["pid_start"], main_runtime_ns=DC.process_namespace_identity())
        self.write_rows()
        database = home / "runtime/data/opencode/opencode.db"
        database.parent.mkdir(parents=True)
        writer = sqlite3.connect(database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE part (id TEXT PRIMARY KEY, data TEXT)")
        writer.commit()
        writer.execute("INSERT INTO part VALUES ('wal-row', 'still readable')")
        writer.commit()
        self.addCleanup(writer.close)
        wal = Path(str(database) + "-wal")
        shm = Path(str(database) + "-shm")
        self.assertGreater(wal.stat().st_size, 0)
        cache = home / "runtime/cache"
        cache.mkdir(parents=True)
        (cache / "entry").write_text("cache")
        os.utime(home, ns=(self.now, self.now))
        with mock.patch.object(R._Context, "home_process_evidence",
                               return_value=("absent", "fixture-process-scan-complete")):
            result = self.sweep(1.01)
        self.assertEqual(result["partial"], 1, result)
        self.assertTrue((home / "worker-home.json").is_file())
        self.assertTrue(wal.is_file() and shm.is_file())
        self.assertFalse(cache.exists())
        sys.path.insert(0, str(ROOT / "tools"))
        import tidy_transcripts
        with tidy_transcripts._opencode_view(database) as reader:
            self.assertEqual(reader.execute("SELECT data FROM part WHERE id='wal-row'").fetchone(),
                             ("still readable",))
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                reader.execute("DELETE FROM part WHERE id='wal-row'")

    def test_partial_retire_journal_resumes_without_removing_retained_home(self):
        attempt = "att-partial-journal-recovery"
        home = self.add(attempt)
        identity = DC.process_launch_identity(os.getpid())
        self.rows[attempt][1].update(
            main_runtime_harness="claude", main_runtime_pid=str(os.getpid()),
            main_runtime_pid_start=identity["pid_start"], main_runtime_ns=DC.process_namespace_identity())
        self.write_rows()
        cache = home / "cache"
        cache.mkdir()
        (cache / "entry").write_text("cache")
        transcript = home / "projects" / "repo" / "session-raw.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"message"}\n')
        (home / "settings.local.json").write_text("{}")
        outside = self.root / "outside-target"
        outside.mkdir()
        (outside / "keep").write_text("not part of the home")
        (home / "plugins-link").symlink_to(outside, target_is_directory=True)
        os.utime(home, ns=(self.now, self.now))
        remove = R._remove_leaf
        with mock.patch.object(R._Context, "home_process_evidence",
                               return_value=("absent", "fixture-process-scan-complete")), \
                mock.patch.object(R, "_remove_leaf", side_effect=lambda *a: "partial"):
            result = self.sweep(1.01)
        self.assertEqual(result["partial"], 1, result)
        retiring = self.root / "homes/worker-retention/retiring"
        names = sorted(path.name for path in retiring.iterdir())
        journals = [name for name in names if name.endswith(".journal")]
        # Each interrupted entry (directory, file, symlink) left its leaf and journal.
        self.assertEqual(len(journals), 3, names)
        self.assertEqual(len(names), 6, names)
        for name in journals:
            record = json.loads((retiring / name).read_text())
            self.assertEqual(record["operation"], "partial-entry")
            self.assertIn(record["source"], {"cache", "settings.local.json", "plugins-link"})
        self.assertTrue(home.is_dir())
        self.assertFalse(any((home / name).exists() or (home / name).is_symlink()
                             for name in ("cache", "settings.local.json", "plugins-link")))
        with mock.patch.object(R._Context, "home_process_evidence",
                               return_value=("absent", "fixture-process-scan-complete")), \
                mock.patch.object(R, "_remove_leaf", wraps=remove):
            resumed = self.sweep(2.02)
        self.assertGreaterEqual(resumed["detached_leftover"], 3)
        self.assertTrue(home.is_dir())
        self.assertFalse(any(retiring.iterdir()))
        self.assertEqual(transcript.read_text(), '{"type":"message"}\n')
        self.assertTrue((home / "worker-home.json").is_file())
        self.assertEqual((outside / "keep").read_text(), "not part of the home")

    def test_pruned_homes_stay_resolvable_through_attempt_viewer(self):
        import sqlite3
        import attempt_view as VIEW
        from claude_project_dir import encode_project_dir
        identity = DC.process_launch_identity(os.getpid())
        main = dict(main_runtime_pid=str(os.getpid()), main_runtime_pid_start=identity["pid_start"],
                    main_runtime_ns=DC.process_namespace_identity())
        claude_attempt, opencode_attempt = "att-viewer-claude", "att-viewer-opencode"
        claude_home = self.add(claude_attempt)
        opencode_home = self.add(opencode_attempt, harness="opencode")
        self.rows[claude_attempt][1].update(main_runtime_harness="claude", **main)
        self.rows[opencode_attempt][1].update(main_runtime_harness="opencode", **main)
        self.write_rows()
        transcript = claude_home / "projects" / encode_project_dir("/repo") / "session-raw.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"message"}\n')
        database = opencode_home / "runtime/data/opencode/opencode.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as writer:
            writer.execute("CREATE TABLE part (id TEXT PRIMARY KEY)")
        for home in (claude_home, opencode_home):
            (home / "cache").mkdir()
            (home / "cache" / "entry").write_text("cache")
            os.utime(home, ns=(self.now, self.now))
        with mock.patch.object(R._Context, "home_process_evidence",
                               return_value=("absent", "fixture-process-scan-complete")):
            result = self.sweep(1.01)
        self.assertEqual(result["partial"], 2, result)
        self.assertFalse((claude_home / "cache").exists() or (opencode_home / "cache").exists())
        log = self.root / "viewer-owner.jsonl"
        log.write_text(json.dumps({"type": "dispatch.supervisor.session-announced",
                                   "session_id": "session-raw", "cwd": "/repo"}) + "\n")
        cases = (
            (claude_attempt, "claude", {"runtime_home": str(claude_home)}, "claude", transcript),
            (opencode_attempt, "opencode", {"runtime_home": str(opencode_home),
                                            "opencode_runtime_dir": str(opencode_home / "runtime")},
             "opencode-db", database),
        )
        for attempt, harness, meta, kind, expected in cases:
            row = (self.root, ["2026", "done", "repo", "/repo", "owner", ""],
                   {"attempt_id": attempt, "harness": harness, "log_file": str(log),
                    "cwd": "/repo", **meta})
            with mock.patch.object(VIEW, "_find_attempt", return_value=row), \
                    mock.patch.object(VIEW, "_log_path", return_value=str(log)):
                sources, error = VIEW.resolve_sources(attempt)
            self.assertIsNone(error)
            native = [source for source in sources if source.kind == kind]
            self.assertEqual([source.path for source in native], [expected], sources)
            with R.worker_home_read_admission(
                    self.jobs, meta["runtime_home"], expected, attempt, harness) as admitted:
                self.assertTrue(admitted)

    def test_live_unknown_dead_and_seven_day_retention_boundary(self):
        live = R._Row("", ["", "done", "", "", "", "harness=claude"])
        identity = DC.process_launch_identity(os.getpid())
        live._meta = {"main_runtime_harness": "claude", "main_runtime_pid": str(os.getpid()),
                      "main_runtime_pid_start": identity["pid_start"],
                      "main_runtime_ns": DC.process_namespace_identity(), "harness": "claude"}
        self.assertTrue(R._retain_raw_for_main(live, self.now, self.now + 99 * R.RAW_UNKNOWN_CAP_NS))
        unknown = R._Row("", ["", "done", "", "", "", "harness=claude"])
        unknown._meta = {"harness": "claude", "main_runtime_harness": "claude",
                         "main_runtime_pid": "2147483647", "main_runtime_pid_start": "1",
                         "main_runtime_ns": "pid:[different]"}
        self.assertTrue(R._retain_raw_for_main(unknown, self.now, self.now + R.RAW_UNKNOWN_CAP_NS - 1))
        self.assertFalse(R._retain_raw_for_main(unknown, self.now, self.now + R.RAW_UNKNOWN_CAP_NS))

    def test_completion_clock_expires_at_exact_retention_boundary(self):
        path = self.add("att-exact-retention-boundary")
        stamp = datetime.fromtimestamp(self.now / 10**9, timezone.utc).isoformat().replace("+00:00", "Z")
        self.set_timestamp("att-exact-retention-boundary", stamp)
        row = R._Snapshot(self.jobs).rows["att-exact-retention-boundary"]
        completion_ns = R._ended_ns(row)
        self.assertIsNotNone(completion_ns)
        os.utime(path, ns=(completion_ns, completion_ns))

        before = R.sweep(self.jobs, now_ns=completion_ns + RETENTION_NS - 1, force=True)
        self.assertEqual(before["removed"], 0)
        self.assertEqual(self.kept(before, "within-window"), 1)
        at_boundary = R.sweep(self.jobs, now_ns=completion_ns + RETENTION_NS, force=True)
        self.assertEqual(at_boundary["removed"], 1)
        self.assertFalse(path.exists())

    def test_expired_terminal_home_retires_after_the_completion_window(self):
        path = self.add("att-a1")
        first = self.sweep(0)
        self.assertTrue(path.is_dir())
        self.assertEqual(first["eligible"], 1)
        self.assertEqual(self.kept(first, "within-window"), 1)
        started = self.state()["eligible_since"][self.key("att-a1")]
        self.assertEqual(started, self.now)
        later = self.sweep((3) / 7)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.state()["eligible_since"][self.key("att-a1")], started)
        self.assertEqual(later["removed"], 0)
        done = self.sweep((8) / 7)
        self.assertEqual(done["removed"], 1)
        self.assertFalse(path.exists())
        self.assertEqual(list((self.root / "homes/worker-retention/retiring").iterdir()), [])
        self.assertNotIn(self.key("att-a1"), self.state()["eligible_since"])
        again = self.sweep((9) / 7)
        self.assertEqual((again["examined"], again["removed"]), (0, 0))

    def test_protection_holds_until_existing_completion_clock_expires(self):
        path = self.add("att-a2")
        self.sweep(0)
        self.set_status("att-a2", "open")
        mid = self.sweep((4) / 7)
        self.assertEqual(self.kept(mid, "not-terminal"), 1)
        self.assertNotIn(self.key("att-a2"), self.state()["eligible_since"])
        self.set_status("att-a2", "done")
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(path.exists())

    def test_old_completion_is_removed_on_first_sweep(self):
        old = self.now - int(10 / 7 * RETENTION_NS)
        log = self.root / "finished.log"
        log.write_text("finished")
        path = self.add("att-old-completion", log_file=str(log))
        self.set_timestamp("att-old-completion", datetime.fromtimestamp(old / 10**9, timezone.utc).isoformat().replace("+00:00", "Z"))
        os.utime(log, ns=(old, old))
        os.utime(path, ns=(old, old))

        result = self.sweep(0)

        self.assertEqual(result["removed"], 1)
        self.assertFalse(path.exists())

    def test_recent_log_mtime_keeps_old_registry_timestamp_within_window(self):
        old = self.now - int(10 / 7 * RETENTION_NS)
        recent = self.now - int(2 / 7 * RETENTION_NS)
        log = self.root / "recent-finish.log"
        log.write_text("finished")
        path = self.add("att-recent-log", log_file=str(log))
        self.set_timestamp("att-recent-log", datetime.fromtimestamp(old / 10**9, timezone.utc).isoformat().replace("+00:00", "Z"))
        os.utime(log, ns=(recent, recent))
        os.utime(path, ns=(old, old))

        result = self.sweep(0)

        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "within-window"), 1)
        self.assertTrue(path.is_dir())

    def test_unparseable_completion_uses_first_seen_clock(self):
        path = self.add("att-unparseable-completion")
        self.set_timestamp("att-unparseable-completion", "not-a-timestamp")

        first = self.sweep(0)

        self.assertEqual(first["removed"], 0)
        self.assertEqual(self.state()["eligible_since"][path.name], self.now)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(path.exists())

    def test_recent_activity_extends_old_completion_window(self):
        old = self.now - int(10 / 7 * RETENTION_NS)
        for suffix in ("supervisor", "view", "home"):
            with self.subTest(activity=suffix):
                attempt = "att-old-activity-" + suffix
                path = self.add(attempt)
                self.set_timestamp(attempt, datetime.fromtimestamp(old / 10**9, timezone.utc).isoformat().replace("+00:00", "Z"))
                recent = self.now - int(RETENTION_NS / 7)
                if suffix == "supervisor":
                    state = self.root / "supervisor-state" / f"{attempt}.json"
                    state.parent.mkdir(exist_ok=True)
                    state.write_text("{}")
                    os.utime(state, ns=(recent, recent))
                elif suffix == "view":
                    views = self.root / "homes" / "worker-retention" / "views"
                    views.mkdir(parents=True, exist_ok=True, mode=0o700)
                    marker = views / path.name
                    marker.write_text("")
                    os.utime(marker, ns=(recent, recent))
                else:
                    os.utime(path, ns=(recent, recent))

                first = self.sweep(0)

                self.assertEqual(first["removed"], 0)
                self.assertEqual(self.kept(first, "within-window"), 1)
                self.assertEqual(self.sweep((5) / 7)["removed"], 0)
                self.assertEqual(self.sweep((8) / 7)["removed"], 1)

    def test_running_and_unknown_status_are_kept(self):
        paths = [self.add("att-open", "open"), self.add("att-run", "running"), self.add("att-odd", "weird")]
        result = self.expire()
        self.assertEqual(self.kept(result, "not-terminal"), 3)
        self.assertTrue(all(path.is_dir() for path in paths))

    def test_open_reference_from_another_open_row_keeps_the_home(self):
        path = self.add("att-parent")
        self.row("att-child", "open", parent_attempt_id="att-parent")
        result = self.expire()
        self.assertEqual(self.kept(result, "open-reference"), 1)
        self.assertTrue(path.is_dir())
        self.set_status("att-child", "done")
        self.assertEqual(self.sweep((9) / 7)["removed"], 1)
        self.assertFalse(path.exists())

    def test_recent_activity_extends_the_window(self):
        path = self.add("att-view")
        self.sweep(0)
        marker = self.root / "homes/worker-retention/views"
        marker.mkdir(exist_ok=True, mode=0o700)
        mark = marker / self.key("att-view")
        mark.write_text("")
        seen = (self.now + int(5 / 7 * RETENTION_NS)) / 1e9
        os.utime(mark, (seen, seen))
        self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.sweep((13) / 7)["removed"], 1)

    def test_split_credential_file_keeps_the_home_and_a_link_does_not(self):
        # A Claude refresh that replaced the generated link leaves a regular file
        # that may hold the only copy of a rotated token; the builder keeps it too.
        split = self.add("att-split")
        (split / ".credentials.json").write_text('{"synthetic": "rotated"}')
        linked = self.add("att-linked")
        main = self.outside / ".credentials.json"
        main.write_text('{"synthetic": "main"}')
        (linked / ".credentials.json").symlink_to(main)
        result = self.expire()
        self.assertEqual(self.kept(result, "split-credential"), 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual((split / ".credentials.json").read_text(), '{"synthetic": "rotated"}')
        self.assertNotIn(self.key("att-split"), self.state()["eligible_since"])
        self.assertFalse(linked.exists())
        self.assertEqual(main.read_text(), '{"synthetic": "main"}')
        (split / ".credentials.json").unlink()
        self.assertEqual(self.sweep((9) / 7)["removed"], 1)
        self.assertFalse(split.exists())

    def test_external_read_only_cache_link_does_not_block_home_removal_or_remove_target(self):
        home = self.add("att-external-cache-link")
        (home / "plugins/cache/catalog").mkdir(parents=True)
        (home / "plugins/cache/catalog/index.json").write_text('{"fixture":true}')
        (home / "cache/remote_plugin_catalog").mkdir(parents=True)
        (home / "cache/remote_plugin_catalog/catalog.json").write_text('{"fixture":true}')

        shared = self.root / "homes/shared/opencode-sdk/lock-id/node_modules"
        shared.mkdir(parents=True)
        payload = shared / "package.js"
        payload.write_text("immutable fixture bytes")
        (home / "node_modules").symlink_to(shared, target_is_directory=True)

        def restore_permissions():
            for parent, dirs, files in os.walk(shared):
                Path(parent).chmod(0o700)
                for name in files:
                    (Path(parent) / name).chmod(0o600)
            shared.chmod(0o700)
        self.addCleanup(restore_permissions)
        for parent, dirs, files in os.walk(shared):
            for name in files:
                (Path(parent) / name).chmod(0o444)
            for name in dirs:
                (Path(parent) / name).chmod(0o555)
        shared.chmod(0o555)

        result = self.expire()

        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())
        self.assertEqual(payload.read_text(), "immutable fixture bytes")
        self.assertEqual(payload.stat().st_mode & 0o222, 0)


class EndedTimestamp(Fixture):
    def _row(self, timestamp, log_file=""):
        metadata = f"log_file={log_file}" if log_file else ""
        return R._Row("", [timestamp, "done", "/repo", "/wt", "att-clock", metadata])

    def test_iso_timestamp_formats_use_utc_and_preserve_zero(self):
        expected = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()) * 10**9
        for timestamp in (
            "2026-10-01T00:00:00Z",
            "2026-10-01T03:00:00+03:00",
            "2026-10-01T00:00:00",
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(R._ended_ns(self._row(timestamp)), expected)
        self.assertEqual(R._ended_ns(self._row("1970-01-01T00:00:00Z")), 0)

    def test_unparseable_or_unrepresentable_timestamp_returns_none(self):
        for timestamp in ("bad", "9999-12-31T23:59:59-23:59"):
            with self.subTest(timestamp=timestamp):
                self.assertIsNone(R._ended_ns(self._row(timestamp)))
        self.assertIsNone(R._ended_ns(self._row(None)))

    def test_regular_log_mtime_only_moves_end_time_later(self):
        log = self.root / "attempt.log"
        log.write_text("log")
        registry_ns = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()) * 10**9
        later_ns = registry_ns + 123456789
        os.utime(log, ns=(later_ns, later_ns))
        self.assertEqual(R._ended_ns(self._row("2026-10-01T00:00:00Z", str(log))), later_ns)
        earlier_ns = registry_ns - int(RETENTION_NS / 7)
        os.utime(log, ns=(earlier_ns, earlier_ns))
        self.assertEqual(R._ended_ns(self._row("2026-10-01T00:00:00Z", str(log))), registry_ns)

    def test_missing_relative_symlink_and_directory_logs_are_ignored(self):
        target = self.root / "target.log"
        target.write_text("target")
        link = self.root / "linked.log"
        link.symlink_to(target)
        directory = self.root / "log-directory"
        directory.mkdir()
        absent = self.root / "absent.log"
        registry_ns = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()) * 10**9
        paths = (str(absent), "relative.log", str(link), str(directory),
                 str(self.root / "invalid\x00.log"))
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(R._ended_ns(self._row("2026-10-01T00:00:00Z", path)), registry_ns)

    def test_unreadable_log_and_unparseable_registry_do_not_supply_time(self):
        log = self.root / "unreadable.log"
        log.write_text("log")
        registry_ns = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()) * 10**9
        original_stat = os.stat

        def deny_log(path, *args, **kwargs):
            if os.fspath(path) == str(log):
                raise PermissionError("fixture permission denial")
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(os, "stat", side_effect=deny_log):
            self.assertEqual(R._ended_ns(self._row("2026-10-01T00:00:00Z", str(log))), registry_ns)
        self.assertIsNone(R._ended_ns(self._row("invalid", str(log))))


class Routes(Fixture):
    def route(self, name="rt-one", *, outcome=True, hash_ok=True):
        record = {"schema_version": 1, "capability": "autopilot-code", "nodes": []}
        record["route_hash"] = route_hash(record)
        record["route_id"] = name
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(record))
        if outcome:
            (self.root / f"{name}.outcome.json").write_text(
                json.dumps({"route_id": name, "route_hash": record["route_hash"] if hash_ok else "x"}))
        return path, record

    def test_live_attempt_process_keeps_home_even_when_route_is_open(self):
        attempt = "att-open-route-resume"
        path, record = self.route(name="rt-open-resume", outcome=False)
        home = self.add(attempt, route_file=str(path), route_id=record["route_id"],
                        route_hash=record["route_hash"])
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env={**os.environ, DC.ATTEMPT_DESCENDANT_ENV: attempt,
                 "HEARTING_WORKER_HOME": str(home), "CLAUDE_CONFIG_DIR": str(home)},
        )
        try:
            result = self.expire()
            self.assertEqual(self.kept(result, "not-quiescent"), 1)
            self.assertTrue(home.is_dir())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_valid_open_route_alone_does_not_keep_home(self):
        path, record = self.route(outcome=False)
        home = self.add("att-r1", route_file=str(path), route_id=record["route_id"], route_hash=record["route_hash"])
        result = self.expire()
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())

    def test_route_closed_exactly_one_generation_only(self):
        path, record = self.route(hash_ok=False)
        self.add("att-r2", route_file=str(path), route_id=record["route_id"], route_hash=record["route_hash"])
        result = self.expire()
        self.assertEqual(self.unknown(result, "route-unknown"), 1)
        self.assertEqual(self.state()["last_result"]["unknown_causes"]["route-unknown"],
                         {"route-binding-mismatch": 1})

    def test_conflicting_route_binding_is_unknown(self):
        path, record = self.route()
        home = self.add("att-r3", route_file=str(path), route_id=record["route_id"], route_hash="sha256:bad")
        result = self.expire()
        self.assertEqual(self.unknown(result, "route-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_route_file_gone_remains_unknown(self):
        home = self.add("att-r4", route_file=str(self.root / "gone.json"), route_id="rt-gone", route_hash="sha256:x")
        first = self.sweep(0)
        self.assertEqual(self.unknown(first, "route-unknown"), 1)
        self.assertTrue(home.is_dir())
        self.assertEqual(self.unknown(self.sweep((8) / 7), "route-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_declared_empty_route_is_not_routeless(self):
        home = self.add("att-empty-route", route_file="")
        self.assertEqual(self.unknown(self.expire(), "route-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_reopened_valid_route_does_not_protect_and_advanced_route_is_unknown(self):
        path, record = self.route()
        home = self.add("att-r5", route_file=str(path), route_id=record["route_id"],
                        route_hash=record["route_hash"])
        self.sweep(0)
        path.with_name(path.stem + ".outcome.json").unlink()
        self.assertEqual(self.sweep(8 / 7)["removed"], 1)
        self.assertFalse(home.exists())
        path, record = self.route(name="rt-advanced")
        home = self.add("att-r6", route_file=str(path), route_id=record["route_id"],
                        route_hash=record["route_hash"])
        self.sweep(0)
        path.write_text(json.dumps({**record, "route_id": "rt-next"}))
        result = self.sweep(8 / 7)
        self.assertEqual(self.unknown(result, "route-unknown"), 1)
        self.assertTrue(home.is_dir())


class Deliveries(Fixture):
    def producer_delivery(self, attempt="att-producer", worker_type="review"):
        route = {}
        if worker_type in ("owner", "frame"):
            path, record = Routes.route(self, name="rt-" + worker_type)
            prefix = "owner_" if worker_type == "owner" else ""
            route = {prefix + "route_file": str(path), prefix + "route_id": record["route_id"],
                     prefix + "route_hash": record["route_hash"]}
            if worker_type == "frame":
                route["route_node"] = "frame"
        home = self.add(
            attempt, status="open", worker_type=worker_type, dispatch_depth="1",
            parent_attempt_id="-", parent_sid="sess-producer",
            parent_completion_delivery="claude-parent-runtime", attempt_schema_version="2",
            transport="headless", execution_surface="registered-headless",
            registered_worker="1", fallback_hop="same-harness-headless", **route)
        self.assertTrue(DC.close_attempt_row(self.jobs, attempt, "completed-marker"))
        fields = self.jobs.read_text().splitlines()[-1].split("\t")
        record = JOIN.materialize_pending_delivery(self.jobs, fields)
        self.assertIsNotNone(record)
        self.assertEqual(json.loads(record.read_text())["parent_attempt_id"], "-")
        return home, record

    def test_real_owner_delivery_retires(self):
        home, record = self.producer_delivery("att-owner-producer", "owner")
        value = json.loads(record.read_text())
        P.claim(self.root, "sess-producer", value["delivery_id"],
                claim_owner="test", lease_seconds=60)
        P.ack(self.root, "sess-producer", value["delivery_id"], acked_by="test")
        self.assertEqual(self.sweep(0)["eligible"], 1)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_real_frame_delivery_retires(self):
        home, record = self.producer_delivery("att-frame-producer", "frame")
        value = json.loads(record.read_text())
        P.claim(self.root, "sess-producer", value["delivery_id"],
                claim_owner="test", lease_seconds=60)
        P.ack(self.root, "sess-producer", value["delivery_id"], acked_by="test")
        self.assertEqual(self.sweep(0)["eligible"], 1)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_producer_sent_ambiguous_record_keeps_home(self):
        home, record = self.producer_delivery("att-ambiguous", "review")
        value = json.loads(record.read_text())
        P.claim(self.root, "sess-producer", value["delivery_id"],
                claim_owner="test", lease_seconds=60)
        P.mark_sent_ambiguous(self.root, "sess-producer", value["delivery_id"],
                              claim_owner="test")
        self.assertEqual(self.sweep(0)["kept"].get("delivery-open"), 1)
        self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())

    def test_real_depth_one_delivery_and_notice_retire_after_retention_window(self):
        home, record = self.producer_delivery()
        notice = NOTICE.notify("claude", "sess-notice", key="retention-fixture",
                               subject="Status", text="Read this", jobs=self.jobs)
        self.assertEqual(len(notice["delivery_id"]), len("delivery-") + 64)
        self.assertTrue(notice["parent_attempt_id"].startswith("notice-"))
        self.assertEqual(self.sweep(0)["kept"].get("delivery-open"), 1)
        value = json.loads(record.read_text())
        P.claim(self.root, "sess-producer", value["delivery_id"],
                claim_owner="test", lease_seconds=60)
        P.ack(self.root, "sess-producer", value["delivery_id"], acked_by="test")
        self.assertEqual(self.sweep(0)["eligible"], 1)
        self.assertEqual(self.sweep((6.99) / 7)["removed"], 0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_mixed_notice_and_attempt_members_protect_actual_worker(self):
        home = self.add("att-mixed")
        notice = NOTICE.notify("claude", "sess-mixed", key="mixed-fixture",
                               subject="Status", text="Read this", jobs=self.jobs)
        self.assertIsNotNone(notice)
        P.create(
            self.root, recipient_kind=notice["recipient_kind"], recipient_key="sess-other",
            delivery_id=notice["delivery_id"], session_generation="",
            session_generation_supported="0",
            attempt_ids=[notice["attempt_ids"][0], "att-mixed"],
            parent_attempt_id=notice["parent_attempt_id"], route_id="route-free",
            route_node="notice", receipt=notice["receipt"],
            receipt_digest=notice["receipt_digest"],
            row_revisions={notice["attempt_ids"][0]: "notice", "att-mixed": "revision"})
        self.assertEqual(self.sweep(0)["kept"].get("delivery-open"), 1)
        self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())

    def test_producer_prune_tombstone_preserves_depth_one_binding(self):
        home, record = self.producer_delivery("att-pruned", "stage")
        value = json.loads(record.read_text())
        P.claim(self.root, "sess-producer", value["delivery_id"],
                claim_owner="test", lease_seconds=60)
        P.ack(self.root, "sess-producer", value["delivery_id"], acked_by="test")
        self.assertEqual(P.prune(self.root, apply=True, now=time.time() + 1,
                                 retention_seconds=0)["pruned_records"], 1)
        tomb = Path(str(record) + ".pruned")
        self.assertEqual(json.loads(tomb.read_text())["parent_attempt_id"], "-")
        self.assertEqual(self.sweep(0)["eligible"], 1)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def intent(self, attempt, **meta):
        digest = "a" * 64
        delivery = "delivery-" + "b" * 32
        meta.setdefault("parent_attempt_id", "att-parent")
        return self.add(attempt, delivery_intent="1", delivery_id=delivery,
                        delivery_recipient_digest=digest, **meta), self.root / "pending-delivery" / digest / f"{delivery}.json"

    def test_intent_without_a_record_or_tombstone_is_pending(self):
        home, _ = self.intent("att-d1")
        result = self.expire()
        self.assertEqual(self.kept(result, "delivery-pending"), 1)
        self.assertTrue(home.is_dir())

    def test_malformed_tombstone_keeps_only_its_home_and_the_pass_completes(self):
        others = [self.add(f"att-other{i}") for i in range(2)]
        home, record = self.intent("att-poison")
        record.parent.mkdir(parents=True)
        Path(str(record) + ".pruned").write_text(json.dumps({
            "schema": P.PRUNE_TOMBSTONE_SCHEMA, "delivery_id": record.name[:-5],
            "recipient_digest": record.parent.name, "attempt_ids": ["att-poison"],
            "parent_attempt_id": "att-parent", "terminal_state": ["acked"]}))
        result = self.expire()
        self.assertEqual(result["removed"], 2)
        self.assertEqual(self.unknown(result, "delivery-unknown"), 1)
        self.assertTrue(home.is_dir())
        self.assertFalse(any(path.exists() for path in others))
        with mock.patch.object(R, "_bind", side_effect=TypeError("unexpected")):
            stalled = self.sweep((9) / 7)
        self.assertEqual(self.unknown(stalled, "examine-error"), 1)
        self.assertEqual(self.state()["last_result"]["unknown"], {"examine-error": 1})
        self.assertTrue(home.is_dir())

    def test_resolved_record_naming_the_attempt_extends_its_window(self):
        recipient = "known-owner-parent"
        home = self.add("att-resolved", worker_type="owner", parent_sid=recipient,
                        parent_completion_delivery="codex-native-queue")
        self.sweep(0)
        import dispatch_supervision as supervision
        record = supervision.materialize(self.jobs, {"att-resolved"}, reason="closure-blocked")[0]
        path = P.record_path(self.root, recipient, record["delivery_id"])
        P.claim(self.root, recipient, record["delivery_id"], claim_owner="test", lease_seconds=60)
        P.ack(self.root, recipient, record["delivery_id"], acked_by="test")
        late = self.now + int(7.9 / 7 * RETENTION_NS)
        os.utime(path, ns=(late, late))
        self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())
        self.assertEqual(self.sweep((15) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_open_record_keeps_and_typed_terminal_record_releases(self):
        home, record = self.intent("att-d2")
        record.parent.mkdir(parents=True)
        self.delivery_record(record, "att-d2", state="pending")
        self.assertEqual(self.expire()["kept"].get("delivery-open"), 1)
        self.delivery_record(record, "att-d2")
        self.assertEqual(self.sweep((9) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_tombstone_releases(self):
        home, record = self.intent("att-d3")
        self.delivery_record(record, "att-d3")
        self.assertEqual(P.prune(self.root, apply=True, now=time.time() + 1,
                                 retention_seconds=0)["pruned_records"], 1)
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)

    def test_zero_byte_legacy_or_mismatched_tombstone_cannot_settle(self):
        home, record = self.intent("att-fake")
        record.parent.mkdir(parents=True)
        tomb = Path(str(record) + ".pruned")
        tomb.write_text("")
        self.assertEqual(self.unknown(self.expire(), "delivery-unknown"), 1)
        tomb.write_text(json.dumps({"schema": P.PRUNE_TOMBSTONE_SCHEMA,
                                    "delivery_id": record.name[:-5],
                                    "recipient_digest": record.parent.name,
                                    "attempt_ids": ["att-other"],
                                    "parent_attempt_id": "att-parent",
                                    "terminal_state": "acked"}))
        self.assertEqual(self.unknown(self.sweep((16) / 7), "delivery-unknown"), 1)
        fake = self.outside / "fake-pruned"
        fake.write_text(json.dumps({**json.loads(tomb.read_text()), "attempt_ids": ["att-fake"]}))
        tomb.unlink()
        tomb.symlink_to(fake)
        self.assertEqual(self.unknown(self.sweep((24) / 7), "delivery-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_unreadable_own_record_is_unknown(self):
        home, record = self.intent("att-d4")
        record.parent.mkdir(parents=True)
        record.write_text("{not json")
        result = self.expire()
        self.assertEqual(self.unknown(result, "delivery-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_wrong_terminal_binding_and_fake_tombstone_never_settle(self):
        home, record = self.intent("att-bind")
        valid = self.delivery_record(record, "att-bind")
        self.sweep(0)
        for window, change in zip((8 / 7, 9 / 7, 10 / 7, 11 / 7, 12 / 7), (
                {"attempt_ids": ["att-other"]},
                {"parent_attempt_id": "att-other"},
                {"recipient_digest": "c" * 64},
                {"delivery_id": "delivery-" + "c" * 32},
                {"schema_version": -1},
        )):
            with self.subTest(change=change):
                record.write_text(json.dumps({**valid, **change}))
                self.assertEqual(self.sweep(window)["removed"], 0)
                self.assertTrue(home.is_dir())
        record.write_text("{")
        tomb = Path(str(record) + ".pruned")
        tomb.symlink_to(self.outside / "fake")
        self.assertEqual(self.sweep((20) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())

    def test_unreadable_group_and_malformed_open_reference_keep_every_home(self):
        home = self.add("att-global")
        self.sweep(0)
        group = self.root / "pending-delivery" / ("d" * 64)
        group.parent.mkdir(exist_ok=True)
        group.symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(self.unknown(self.sweep((8) / 7), "delivery-index-unknown"), 1)
        group.unlink()
        group.mkdir()
        (group / ("delivery-" + "a" * 32 + ".json")).write_text(
            json.dumps({"state": "claimed", "attempt_ids": "att-global"}))
        self.assertEqual(self.unknown(self.sweep((16) / 7), "delivery-index-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_record_naming_the_attempt_keeps_it_even_without_an_intent(self):
        home = self.add("att-d5")
        folder = self.root / "pending-delivery" / ("c" * 64)
        folder.mkdir(parents=True)
        self.delivery_record(folder / ("delivery-" + "d" * 32 + ".json"),
                             "att-other", "att-d5", state="claimed")
        result = self.expire()
        self.assertEqual(self.kept(result, "delivery-open"), 1)
        self.assertTrue(home.is_dir())

    def test_a_childs_undelivered_completion_keeps_the_parent_home(self):
        home = self.add("att-d8")
        digest, delivery = "a" * 64, "delivery-" + "7" * 32
        self.row("att-kid", "done", parent_attempt_id="att-d8", delivery_intent="1",
                 delivery_id=delivery, delivery_recipient_digest=digest)
        result = self.expire()
        self.assertEqual(self.kept(result, "delivery-pending"), 1)
        self.assertTrue(home.is_dir())
        folder = self.root / "pending-delivery" / digest
        folder.mkdir(parents=True)
        self.delivery_record(folder / (delivery + ".json"), "att-kid", "att-d8")
        self.assertEqual(self.sweep((9) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_unattributable_corrupt_record_remains_unknown(self):
        home = self.add("att-d6")
        folder = self.root / "pending-delivery" / ("c" * 64)
        folder.mkdir(parents=True)
        corrupt = folder / ("delivery-" + "e" * 32 + ".json")
        corrupt.write_text("{")
        stamp = self.now / 1e9
        os.utime(corrupt, (stamp, stamp))
        self.sweep(0)
        self.assertEqual(self.unknown(self.sweep((8) / 7), "delivery-index-unknown"), 1)
        self.assertTrue(home.exists())

    def test_oversized_index_keeps_everything_that_pass(self):
        home = self.add("att-d7")
        with mock.patch.object(R, "MAX_DELIVERY_RECORDS", 1):
            folder = self.root / "pending-delivery" / ("c" * 64)
            folder.mkdir(parents=True)
            for index in range(3):
                (folder / f"delivery-{index:032x}.json").write_text(json.dumps({"state": "acked"}))
            result = self.expire()
        self.assertEqual(self.unknown(result, "delivery-index-unknown"), 1)
        self.assertTrue(home.is_dir())


class Recovery(Fixture):
    def reserve(self, attempt, family="f" * 64, claim=None):
        base = self.root / "automatic-replacements"
        (base / "by-source").mkdir(parents=True, exist_ok=True)
        (base / "claims").mkdir(parents=True, exist_ok=True)
        (base / "by-source" / f"{attempt}.json").write_text(json.dumps(
            {"schema": 1, "original_attempt_id": attempt, "family_id": family}))
        if claim is not None:
            (base / "claims" / f"{family}.json").write_text(json.dumps(claim))

    def test_reservation_before_the_claim_record_keeps_the_home(self):
        home = self.add("att-v1")
        self.reserve("att-v1")
        result = self.expire()
        self.assertEqual(self.kept(result, "replacement-unresolved"), 1)
        self.assertTrue(home.is_dir())

    def test_claim_whose_successor_row_does_not_exist_yet_keeps_the_home(self):
        home = self.add("att-v2")
        self.reserve("att-v2", claim={"original_attempt_id": "att-v2", "replacement_attempt_id": "att-new"})
        self.assertEqual(self.expire()["kept"].get("replacement-unresolved"), 1)
        self.assertTrue(home.is_dir())
        self.row("att-new", "running")
        self.assertEqual(self.sweep((9) / 7)["kept"].get("replacement-unresolved"), 1)

    def test_resolved_claim_releases_the_home(self):
        self.add("att-v3")
        self.reserve("att-v3", claim={"original_attempt_id": "att-v3", "replacement_attempt_id": "att-new"})
        self.row("att-new", "done")
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)

    def test_terminal_successor_open_route_alone_releases_source_home(self):
        source = self.add("att-source")
        self.reserve("att-source", claim={"original_attempt_id": "att-source",
                                          "replacement_attempt_id": "att-successor"})
        route = self.root / "successor.json"
        value = {"schema_version": 1, "capability": "autopilot-code", "nodes": []}
        value["route_hash"] = route_hash(value)
        value["route_id"] = "rt-successor"
        route.write_text(json.dumps(value))
        self.row("att-successor", "done", route_file=str(route), route_id="rt-successor",
                 route_hash=value["route_hash"])
        self.assertEqual(self.expire()["removed"], 1)
        self.assertFalse(source.exists())

    def test_terminal_successor_with_open_delivery_keeps_source_home(self):
        source = self.add("att-source-delivery")
        self.reserve("att-source-delivery", claim={"original_attempt_id": "att-source-delivery",
                                                    "replacement_attempt_id": "att-successor-delivery"})
        path, record = Routes.route(self, name="rt-successor-delivery")
        self.row("att-successor-delivery", "done", route_file=str(path), route_id=record["route_id"],
                 route_hash=record["route_hash"], delivery_intent="1",
                 delivery_id="delivery-" + "a" * 32,
                 delivery_recipient_digest="b" * 64, parent_attempt_id="att-other")
        self.assertEqual(self.kept(self.expire(), "replacement-unresolved"), 1)
        self.assertTrue(source.is_dir())

    def test_malformed_reference_evidence_is_preserved(self):
        for index, body in enumerate(("{", json.dumps({"original_attempt_id": "att-other", "family_id": "f" * 64}),
                                      json.dumps({"original_attempt_id": "att-v4", "family_id": "short"}))):
            attempt = f"att-v4{index}"
            self.add(attempt)
            base = self.root / "automatic-replacements" / "by-source"
            base.mkdir(parents=True, exist_ok=True)
            (base / f"{attempt}.json").write_text(body.replace("att-v4", attempt))
        result = self.expire()
        self.assertEqual(self.unknown(result, "replacement-unknown"), 3)

    def test_recovery_attention_reservation_and_needs_attention_keep_the_home(self):
        bad = self.add("att-v5")
        good = self.add("att-v6")
        folder = self.root / "recovery-attention" / "by-source"
        folder.mkdir(parents=True)
        (folder / "att-v5.json").write_text("[")
        (folder / "att-v6.json").write_text(json.dumps({"original_attempt_id": "att-v6", "recovery_id": "r"}))
        stamp = self.now / 1e9
        os.utime(folder / "att-v6.json", (stamp, stamp))
        self.sweep(0)
        result = self.sweep((8) / 7)
        self.assertEqual(self.unknown(result, "replacement-unknown"), 1)
        self.assertEqual(self.kept(result, "replacement-unresolved"), 1)
        self.assertTrue(bad.is_dir())
        self.assertTrue(good.is_dir())
        import hashlib
        detail = self.root / "recovery-attention" / (hashlib.sha256(b"r").hexdigest() + ".json")
        detail.write_text(json.dumps({"schema": "recovery-attention-v1",
                                      "original_attempt_id": "att-v6", "recovery_id": "r",
                                      "state": "needs-attention", "reason": "receipt-unavailable"}))
        self.assertEqual(self.kept(self.sweep((16) / 7), "replacement-unresolved"), 1)
        self.assertTrue(good.is_dir())

    def test_supervisor_state_leftover_extends_the_window_only(self):
        self.add("att-v7")
        state = self.root / "supervisor-state"
        state.mkdir()
        leftover = state / "att-v7.json"
        leftover.write_text("{}")
        stamp = (self.now + int(6 / 7 * RETENTION_NS)) / 1e9
        os.utime(leftover, (stamp, stamp))
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertEqual(self.sweep((14) / 7)["removed"], 1)


class Liveness(Fixture):
    def dead_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def test_held_supervisor_lease_keeps_and_free_lease_releases(self):
        lease = DC.supervisor_lease_path(self.jobs, "att-l1")
        lease.parent.mkdir(parents=True)
        lease.write_text("x")
        home = self.add("att-l1", supervisor_lease_file=str(lease), supervisor_lease=DC.SUPERVISOR_LEASE_KIND)
        fd = os.open(lease, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            result = self.expire()
            self.assertEqual(self.kept(result, "lease-held"), 1)
            self.assertTrue(home.is_dir())
        finally:
            os.close(fd)
        self.assertEqual(self.sweep((9) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_lease_payload_mismatch_does_not_hide_a_holder(self):
        # supervisor_lease_is_held() answers False for any payload it does not
        # recognise, even while another process holds the file.
        lease = DC.supervisor_lease_path(self.jobs, "att-l2")
        lease.parent.mkdir(parents=True)
        lease.write_text("a different payload")
        self.add("att-l2", supervisor_lease_file=str(lease), supervisor_lease=DC.SUPERVISOR_LEASE_KIND)
        fd = os.open(lease, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.kept(self.expire(), "lease-held"), 1)
        finally:
            os.close(fd)

    def test_noncanonical_or_linked_lease_is_unknown(self):
        target = self.outside / "lease"
        target.write_text("")
        home = self.add("att-l3", supervisor_lease_file=str(target), supervisor_lease=DC.SUPERVISOR_LEASE_KIND)
        link = DC.supervisor_lease_path(self.jobs, "att-l4")
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        other = self.add("att-l4", supervisor_lease_file=str(link), supervisor_lease=DC.SUPERVISOR_LEASE_KIND)
        result = self.expire()
        self.assertEqual(self.unknown(result, "lease-unknown"), 2)
        self.assertTrue(home.is_dir() and other.is_dir())

    def test_review_worker_lease_keeps_a_held_worker(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        nonce = "n" * 32
        path = DC.review_governed_lease_path(artifacts, "cyc_1234", "att-l5")
        path.parent.mkdir(parents=True)
        path.write_bytes(DC.review_governed_lease_payload("att-l5", "cyc_1234", nonce))
        home = self.add("att-l5", artifact_root=str(artifacts), review_cycle_id="cyc_1234",
                        review_governed_lease=DC.REVIEW_GOVERNED_LEASE_KIND, review_governed_lease_nonce=nonce)
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.kept(self.expire(), "lease-held"), 1)
            self.assertTrue(home.is_dir())
        finally:
            os.close(fd)

    def test_unverifiable_summary_owner_is_not_dead(self):
        home = self.add("att-s1", summary_owner_pid="123")  # no start, namespace or state file
        result = self.expire()
        self.assertEqual(self.unknown(result, "summary-owner-unknown"), 1)
        self.assertEqual(self.state()["last_result"]["unknown_causes"]["summary-owner-unknown"],
                         {"identity-incomplete": 1})
        self.assertTrue(home.is_dir())

    def test_live_identity_without_a_vouching_state_file_is_unknown(self):
        me = DC.process_launch_identity(os.getpid())
        home = self.add("att-s2", summary_owner_pid=str(os.getpid()), summary_owner_pid_start=me["pid_start"],
                        summary_owner_pid_observer_ns=DC.process_namespace_identity(),
                        summary_state_file=str(self.root / "owner.json"))
        self.assertEqual(self.unknown(self.expire(), "summary-owner-unknown"), 1)
        self.assertTrue(home.is_dir())

    def test_unknown_owner_namespace_with_valid_free_locks_releases_home(self):
        home = self.add("att-s4")
        owner_lock, _provider = self.attach_summary_owner("att-s4", home)
        foreign = "pid:[4026539999]"
        row_meta = self.rows["att-s4"][1]
        row_meta.update(summary_owner_pid="4242", summary_owner_pid_start="1",
                        summary_owner_pid_observer_ns=foreign)
        self.write_rows()
        state_path = Path(row_meta["summary_state_file"])
        state = json.loads(state_path.read_text())
        state.update(pid=4242, proc_start="1", observer_namespace=foreign)
        state_path.write_text(json.dumps(state))
        self.assertTrue(owner_lock.is_file())
        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=False):
            result = self.expire()
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())

    def test_provably_gone_summary_owner_releases(self):
        home = self.add("att-s3")
        self.attach_summary_owner("att-s3", home)
        self.assertEqual(self.expire()["removed"], 1)
        self.assertFalse(home.exists())

    def test_exact_free_summary_locks_allow_home_retirement(self):
        home = self.add("att-summary-free")
        self.attach_summary_owner("att-summary-free", home)
        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True):
            self.assertEqual(self.expire()["removed"], 1)
        self.assertFalse(home.exists())

    def test_real_process_receipt_releases_after_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        identity = DC.process_launch_identity(child.pid)
        home = self.add("att-real-receipt")
        self.attach_summary_owner("att-real-receipt", home)
        metadata = self.rows["att-real-receipt"][1]
        metadata.update(summary_owner_pid=str(child.pid),
                        summary_owner_pid_start=identity["pid_start"],
                        summary_owner_pid_observer_ns=DC.process_namespace_identity())
        self.write_rows()
        state_path = Path(metadata["summary_state_file"])
        state = json.loads(state_path.read_text())
        state.update(pid=child.pid, proc_start=identity["pid_start"],
                     observer_namespace=DC.process_namespace_identity())
        state_path.write_text(json.dumps(state))
        child.wait()
        self.sweep(0)
        self.assertEqual(self.sweep(8 / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_held_owner_and_old_provider_lockdir_keep_home(self):
        owner_home = self.add("att-summary-owner-lock")
        owner_lock, _provider_lock = self.attach_summary_owner("att-summary-owner-lock", owner_home)
        owner_fd = os.open(owner_lock, os.O_RDWR)
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX)
            self.assertEqual(self.kept(self.expire(), "summary-live"), 1)
        finally:
            os.close(owner_fd)

        provider_home = self.add("att-summary-provider-lock")
        _owner_lock, provider_lock = self.attach_summary_owner("att-summary-provider-lock", provider_home)
        provider_lock.parent.mkdir(parents=True, exist_ok=True)
        provider_lock.mkdir()
        old = time.time() - 30 * 86400
        os.utime(provider_lock, (old, old))
        self.assertEqual(self.kept(self.expire(), "summary-live"), 1)
        self.assertTrue(provider_home.is_dir())

    def test_live_or_unverifiable_quiescence_keeps(self):
        live = self.add("att-q1")
        unknown = self.add("att-q2")
        real = DC.attempt_process_quiescence

        def fake(meta, *, terminal_receipt=False):
            if meta["attempt_id"] == "att-q1":
                return DC.ProcessQuiescence("live", "attempt-descendant-live")
            return DC.ProcessQuiescence("unverifiable", "process-namespace-unverifiable")

        with mock.patch.object(DC, "attempt_process_quiescence", fake):
            result = self.expire()
        self.assertEqual(self.kept(result, "not-quiescent"), 1)
        self.assertEqual(self.unknown(result, "quiescence-unverifiable"), 1)
        self.assertEqual(self.state()["last_result"]["unknown_causes"]["quiescence-unverifiable"],
                         {"process-namespace-unverifiable": 1})
        self.assertTrue(live.is_dir() and unknown.is_dir())
        self.assertIsNotNone(real)

    def test_process_evidence_without_receipt_is_checked_for_real(self):
        pid = str(self.dead_pid())
        gone = self.add("att-q3", launch_outcome="", pid=pid, pid_start="1", pgid=pid)
        observed = DC.attempt_process_quiescence(self.rows["att-q3"][1], terminal_receipt=True)
        # Use one real host observation for both passes; unrelated /proc changes
        # between them must not turn this into a timing-dependent assertion.
        with mock.patch.object(DC, "attempt_process_quiescence", return_value=observed):
            self.sweep(0)
            result = self.sweep((8) / 7)
        if observed.state == "quiescent":
            self.assertEqual(result["removed"], 1)
            self.assertFalse(gone.exists())
        else:
            self.assertIn(observed.state, ("live", "unknown", "unverifiable"))
            bucket = "kept" if observed.state == "live" else "unknown"
            reason = "not-quiescent" if observed.state == "live" else "quiescence-unverifiable"
            self.assertEqual(result[bucket].get(reason), 1)
            self.assertTrue(gone.is_dir())


class Races(Fixture):
    def _optional_sweep(self, days):
        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                mock.patch.object(R._Context, "home_process_evidence",
                                  return_value=("absent", "fixture-process-scan-complete")):
            return self.sweep(days)

    def test_normal_publisher_wins_before_final_admission_for_new_and_existing_groups(self):
        original_broad = R._late_delivery_change
        real_open = AR._open_locked
        active = {"attempt": None, "relation": None, "started": False}
        with mock.patch.object(R, "_late_delivery_change") as broad_mock, \
                mock.patch.object(AR, "_open_locked") as open_mock:
            broad_mock.side_effect = original_broad
            open_mock.side_effect = real_open

            def broad_then_publish(ctx, attempt, deadline):
                result = original_broad(ctx, attempt, deadline)
                if attempt == active["attempt"] and not active["started"]:
                    active["started"] = True
                    admission_entered, done, errors = threading.Event(), threading.Event(), []
                    index = int(attempt.rsplit("-", 1)[1])
                    recipient = f"normal-publisher-{index}"
                    delivery_id = f"delivery-{index + 10:032x}"
                    if index % 2:
                        self.publish_delivery(
                            f"delivery-{index + 100:032x}", recipient,
                            [f"att-existing-unrelated-{index}"], "att-existing-parent")

                    def tracked_open(path, *, blocking):
                        if threading.current_thread().name == "normal-publisher":
                            admission_entered.set()
                        return real_open(path, blocking=blocking)

                    open_mock.side_effect = tracked_open

                    def publish():
                        try:
                            if active["relation"] == "attempt":
                                ids, parent = [attempt], "att-normal-parent"
                            else:
                                ids, parent = [f"att-normal-unrelated-{index}"], attempt
                            self.publish_delivery(delivery_id, recipient, ids, parent)
                        except Exception as exc:  # surfaced on the sweep thread
                            errors.append(repr(exc))
                        finally:
                            done.set()

                    thread = threading.Thread(target=publish, name="normal-publisher")
                    thread.start()
                    if not admission_entered.wait(3) or not done.wait(3):
                        errors.append("normal writer did not reach and finish publication")
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    record = P.record_path(self.root, recipient, delivery_id)
                    self.assertTrue(record.is_file(), "normal API record must be durable before final admission")
                    self.assertEqual(json.loads(record.read_text())["state"], "pending")
                    self.assertEqual(errors, [])
                    open_mock.side_effect = real_open
                return result

            broad_mock.side_effect = broad_then_publish
            for index, (existing_group, relation) in enumerate((
                    (False, "attempt"), (True, "parent"),
                    (False, "parent"), (True, "attempt"))):
                attempt = f"att-normal-publisher-{index}"
                home = self.add(attempt)
                recipient = f"normal-publisher-{index}"
                if existing_group:
                    self.publish_delivery(
                        f"delivery-{index + 100:032x}", recipient,
                        [f"att-existing-unrelated-{index}"], "att-existing-parent")
                active.update(attempt=attempt, relation=relation, started=False)
                self.sweep(0)
                result = self.sweep((8) / 7)
                self.assertTrue(active["started"], f"publisher did not run for {attempt}")
                self.assertTrue(home.is_dir())
                self.assertEqual(result["removed"], 0)
                self.assertGreaterEqual(
                    result["kept"].get("late-evidence-changed", 0), 1,
                    f"{relation} reference from {'existing' if existing_group else 'new'} group was missed",
                )

    def test_detach_admission_orders_normal_publishers_after_rename(self):
        original_final = R._late_candidate_delivery_change
        real_flock = AR.fcntl.flock
        real_rename = R.os.rename
        active = {"attempt": None, "calls": 0}
        for index, (existing_group, relation) in enumerate((
                (False, "attempt"), (True, "parent"),
                (False, "parent"), (True, "attempt"))):
            attempt = f"att-detach-wins-{index}"
            home = self.add(attempt)
            recipient = f"detach-wins-{index}"
            if existing_group:
                self.publish_delivery(
                    f"delivery-{index + 200:032x}", recipient,
                    [f"att-detach-unrelated-{index}"], "att-detach-parent")
            delivery_id = f"delivery-{index + 300:032x}"
            record = P.record_path(self.root, recipient, delivery_id)
            self.sweep(0)
            start_writer, writer_attempted, writer_done = (
                threading.Event(), threading.Event(), threading.Event())
            order, errors = [], []
            active.update(attempt=attempt, calls=0)

            def observed_flock(fd, operation):
                if (threading.current_thread().name == "normal-publisher"
                        and operation & fcntl.LOCK_EX):
                    writer_attempted.set()
                return real_flock(fd, operation)

            def final_check(ctx, selected, deadline):
                if selected == active["attempt"]:
                    active["calls"] += 1
                    if active["calls"] == 2:
                        start_writer.set()
                        if not writer_attempted.wait(3):
                            raise AssertionError("normal publisher never attempted reference admission")
                        value = original_final(ctx, selected, deadline)
                        self.assertFalse(record.exists(), "blocked writer became durable before the final proof/rename")
                        return value
                return original_final(ctx, selected, deadline)

            def observed_rename(*args, **kwargs):
                if threading.current_thread().name == "MainThread":
                    self.assertFalse(record.exists(), "related publisher landed before HOME rename")
                    order.append("rename")
                return real_rename(*args, **kwargs)

            def publish():
                if not start_writer.wait(3):
                    errors.append("final admission was not reached")
                    writer_done.set()
                    return
                try:
                    if relation == "attempt":
                        ids, parent = [attempt], "att-detach-parent"
                    else:
                        ids, parent = [f"att-detach-unrelated-{index}"], attempt
                    self.publish_delivery(delivery_id, recipient, ids, parent)
                    order.append("publish")
                except Exception as exc:
                    errors.append(repr(exc))
                finally:
                    writer_done.set()

            thread = threading.Thread(target=publish, name="normal-publisher")
            thread.start()
            try:
                with mock.patch.object(AR.fcntl, "flock", observed_flock), \
                        mock.patch.object(R, "_late_candidate_delivery_change", final_check), \
                        self.replaced("rename", observed_rename):
                    result = self.sweep(8 / 7)
                    self.assertTrue(writer_done.wait(5), "normal publisher did not finish after detach")
            finally:
                start_writer.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(writer_done.is_set())
            self.assertIn("removed", result, result)
            self.assertEqual(result["removed"], 1, result)
            self.assertFalse(home.exists())
            self.assertTrue(record.is_file())
            self.assertEqual(json.loads(record.read_text())["state"], "pending")
            self.assertEqual(order, ["rename", "publish"])

    def test_unrelated_normal_publishers_run_during_candidate_admission_and_home_is_removed(self):
        home = self.add("att-unrelated-normal-publisher")
        self.sweep(0)
        original_final = R._late_candidate_delivery_change
        real_open = AR._open_locked
        start_writer, admission_attempted, writer_done = (
            threading.Event(), threading.Event(), threading.Event())
        errors = []
        check_calls = []
        completed_inside = []

        def tracked_open(path, *, blocking):
            if threading.current_thread().name == "unrelated-normal-publisher":
                admission_attempted.set()
            return real_open(path, blocking=blocking)

        def publish_many():
            if not start_writer.wait(3):
                errors.append("final admission was not reached")
                writer_done.set()
                return
            try:
                for index in range(3):
                    self.publish_delivery(
                        f"delivery-{index + 500:032x}", "unrelated-normal-publisher",
                        [f"att-unrelated-normal-{index}"], "att-unrelated-normal-parent")
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                writer_done.set()

        thread = threading.Thread(target=publish_many, name="unrelated-normal-publisher")
        thread.start()

        def final_check(ctx, attempt, deadline):
            if attempt == "att-unrelated-normal-publisher":
                check_calls.append(True)
                if len(check_calls) == 2:
                    start_writer.set()
                    self.assertTrue(admission_attempted.wait(3), "normal publisher never reached its write admission")
                    completed_inside.append(writer_done.wait(0.10))
            return original_final(ctx, attempt, deadline)

        try:
            with mock.patch.object(AR, "_open_locked", tracked_open), \
                    mock.patch.object(R, "_late_candidate_delivery_change", final_check):
                result = self.sweep((8) / 7)
        finally:
            start_writer.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(completed_inside, [True], "unrelated publication must complete under candidate admission")
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())
        self.assertEqual(len(list(P.record_directory(self.root, "unrelated-normal-publisher").glob("*.json"))), 3)

    def test_related_reference_contention_defers_and_next_sweep_retries_automatically(self):
        home = self.add("att-reference-busy")
        self.sweep(0)
        first = self.state()["eligible_since"][home.name]
        with AR.admission(self.root, ["att-reference-busy"]):
            result = self.sweep((8) / 7)
            self.assertEqual(result["removed"], 0)
            self.assertEqual(self.kept(result, "delivery-reference-busy"), 1)
            self.assertTrue(home.is_dir())
            self.assertEqual(self.state()["eligible_since"][home.name], first)
        result = self.sweep((8.1) / 7)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())

    def _assert_final_admission_write_keeps_home(self, home, write_record):
        admitted, written = threading.Event(), threading.Event()
        errors = []

        def writer():
            if not admitted.wait(5):
                errors.append("second registry admission barrier timed out")
                written.set()
                return
            try:
                write_record()
            except Exception as exc:  # reported on the test thread
                errors.append(repr(exc))
            finally:
                written.set()

        real_admission = R._registry_lock_until
        admission_count = []

        @contextlib.contextmanager
        def final_admission(jobs, deadline):
            admission_count.append(None)
            with real_admission(jobs, deadline) as acquired:
                if len(admission_count) == 2 and acquired is not False:
                    admitted.set()
                    if not written.wait(5):
                        raise AssertionError("delivery writer did not finish before final body")
                yield acquired

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with mock.patch.object(R, "_registry_lock_until", final_admission):
                result = self.sweep((8) / 7)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(admission_count), 2)
        self.assertEqual(errors, [])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def _assert_late_discovered_record_rewrite_keeps_home(self, attempt_field):
        attempt = "att-late-discovered-" + attempt_field
        home = self.add(attempt)
        self.sweep(0)
        group = self.root / "pending-delivery" / ("f" * 64)
        record = group / ("delivery-" + "9" * 32 + ".json")
        initial = {}
        late_reads = []
        prepared = []
        inserted = []
        admitted, written = threading.Event(), threading.Event()
        errors = []

        def discover_after_initial_index(ctx, candidate, deadline):
            self.assertNotIn(record, ctx.discovery_stamps)
            self.assertNotIn(group, ctx.group_stamps)
            self.assertNotIn(group.name, ctx.delivery_groups)
            self.delivery_record(record, "att-late-other", "att-late-parent", state="acked")
            inserted.append(True)
            result = real_late(ctx, candidate, deadline)
            initial["record"] = json.loads(record.read_text(encoding="utf-8"))
            return result

        real_late = R._late_delivery_change
        real_relation = R._delivery_record_relation
        real_prepare = R._prepare_candidate_delivery_baseline

        def observed_relation(path, related, binding=None):
            result = real_relation(path, related, binding)
            if Path(path) == record:
                late_reads.append(result)
            return result

        def observed_prepare(ctx, candidate, deadline):
            baseline = real_prepare(ctx, candidate, deadline)
            if baseline is not None:
                prepared.append(baseline)
            return baseline

        def writer():
            if not admitted.wait(5):
                errors.append("second registry admission barrier timed out")
                written.set()
                return
            try:
                value = dict(initial["record"], state="pending")
                if attempt_field == "parent_attempt_id":
                    value[attempt_field] = attempt
                else:
                    value[attempt_field] = [attempt]
                record.write_text(json.dumps(value), encoding="utf-8")
            except Exception as exc:  # reported on the test thread
                errors.append(repr(exc))
            finally:
                written.set()

        real_admission = R._registry_lock_until
        admission_count = []

        @contextlib.contextmanager
        def final_admission(jobs, deadline):
            admission_count.append(None)
            with real_admission(jobs, deadline) as acquired:
                if len(admission_count) == 2 and acquired is not False:
                    if not inserted or "unrelated" not in late_reads or not prepared:
                        raise AssertionError("late discovery or baseline preparation did not finish before admission")
                    admitted.set()
                    if not written.wait(5):
                        raise AssertionError("late-discovered record writer missed final admission")
                yield acquired

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with mock.patch.object(R, "_late_delivery_change", discover_after_initial_index), \
                    mock.patch.object(R, "_delivery_record_relation", observed_relation), \
                    mock.patch.object(R, "_prepare_candidate_delivery_baseline", observed_prepare), \
                    mock.patch.object(R, "_registry_lock_until", final_admission):
                result = self.sweep((8) / 7)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(admission_count), 2)
        self.assertEqual(errors, [])
        self.assertEqual(late_reads[0], "unrelated")
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def two(self):
        return self.add("att-x1"), self.add("att-x2")

    def test_log_activity_before_preparation_or_lock_extends_window(self):
        for phase in ("preparation", "lock"):
            with self.subTest(phase=phase):
                old = self.now - int(10 / 7 * RETENTION_NS)
                recent = self.now - int(RETENTION_NS / 7)
                log = self.root / (phase + ".log")
                log.write_text("finished")
                attempt = "att-late-log-" + phase
                home = self.add(attempt, log_file=str(log))
                self.set_timestamp(attempt, datetime.fromtimestamp(
                    old / 10**9, timezone.utc).isoformat())
                os.utime(log, ns=(old, old))
                os.utime(home, ns=(old, old))

                def advance():
                    os.utime(log, ns=(recent, recent))

                if phase == "preparation":
                    original = R._detach

                    def detach(*args, **kwargs):
                        advance()
                        return original(*args, **kwargs)

                    with mock.patch.object(R, "_detach", detach):
                        result = self.sweep(0)
                    reason = "late-activity"
                else:
                    with self.landing(advance, key=home.name) as fired:
                        result = self.sweep(0)
                    self.assertTrue(fired)
                    reason = "late-evidence-changed"

                self.assertEqual(result["removed"], 0)
                self.assertEqual(self.kept(result, reason), 1)
                self.assertTrue(home.is_dir())
                self.assertEqual(self.sweep((5) / 7)["removed"], 0)
                self.assertEqual(self.sweep((8) / 7)["removed"], 1)
                self.assertFalse(home.exists())

    def test_unrelated_append_defers_without_restarting_established_clock(self):
        # Obsolete expectation retained by name for issue #592 traceability:
        # an unrelated complete append no longer defers the due candidate.
        home = self.add("att-append")
        self.sweep(0)
        key = home.name
        first = self.state()["eligible_since"][key]
        with self.landing(lambda: self.append_row("att-unrelated", "done")):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())
        self.assertNotIn(key, self.state()["eligible_since"])
        self.assertEqual(first, self.now)

    def test_unrelated_atomic_registry_replacement_does_not_defer_detach(self):
        home = self.add("att-atomic-registry")
        self.sweep(0)

        def replace_registry():
            timestamp = datetime.fromtimestamp(
                self.now / 10**9, timezone.utc
            ).isoformat().replace("+00:00", "Z")
            line = (f"{timestamp}\tdone\t/repo\t/wt\tatt-atomic-noise\t"
                    "attempt_id=att-atomic-noise\n").encode()
            temp = self.root / "jobs.replacement"
            temp.write_bytes(self.jobs.read_bytes() + line)
            os.replace(temp, self.jobs)

        with self.landing(replace_registry):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())

    def test_producer_bound_recipient_membership_catches_late_gate_record(self):
        recipient = "owner-parent-session"
        digest = P.recipient_digest(recipient)
        group = self.root / "pending-delivery" / digest
        home = self.add("att-gate-owner", worker_type="owner", parent_sid=recipient)
        self.sweep(0)
        def publish():
            self.delivery_record(group / ("delivery-" + "a" * 32 + ".json"),
                                 "att-gate-owner", "att-gate-owner", state="pending")
        with self.landing(publish):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["kept"].get("late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())
        self.assertEqual(self.sweep((8.1) / 7)["kept"].get("delivery-open"), 1)
        self.assertNotIn(home.name, self.state()["eligible_since"])

    def test_known_existing_recipient_group_catches_terminal_supervision(self):
        import dispatch_supervision as supervision
        recipient = "known-owner-parent"
        home = self.add("att-supervision-owner", worker_type="owner", parent_sid=recipient,
                        parent_completion_delivery="codex-native-queue")
        group = P.record_directory(self.root, recipient)
        group.mkdir(parents=True)
        (group / ("delivery-" + "0" * 32 + ".json.pruned")).touch()
        self.sweep(0)
        with self.landing(lambda: supervision.materialize(
                self.jobs, {"att-supervision-owner"}, reason="closure-blocked")):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["kept"].get("late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_renewed_reference_keeps_home_until_completion_clock_expires(self):
        home = self.add("att-renewed")
        self.sweep(0)
        with self.landing(lambda: self.row("att-ref", "open", parent_attempt_id="att-renewed")):
            self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertNotIn(home.name, self.state()["eligible_since"])
        self.set_status("att-ref", "done")
        self.assertEqual(self.sweep((8.1) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_unreadable_preparation_preserves_established_clock(self):
        home = self.add("att-unreadable-prep")
        self.sweep(0)
        first = self.state()["eligible_since"][home.name]
        original = R._Snapshot
        calls = []
        def unreadable_second(jobs):
            calls.append(True)
            if len(calls) == 2:
                raise R._Unreadable("registry")
            return original(jobs)
        with mock.patch.object(R, "_Snapshot", side_effect=unreadable_second):
            result = self.sweep((8) / 7)
        self.assertEqual(result["kept"].get("registry-unreadable"), 1)
        self.assertEqual(self.state()["eligible_since"][home.name], first)
        self.assertEqual(self.sweep((8.1) / 7)["removed"], 1)

    def test_unreadable_global_registry_defers_without_losing_clock(self):
        home = self.add("att-unreadable-global")
        self.sweep(0)
        first = self.state()["eligible_since"][home.name]
        with mock.patch.object(R, "_Snapshot", side_effect=R._Unreadable("registry")):
            self.assertEqual(self.sweep((8) / 7), {"skipped": "registry-unreadable"})
        self.assertEqual(self.state()["eligible_since"][home.name], first)
        self.assertEqual(self.sweep((8.1) / 7)["removed"], 1)


    def test_open_reference_that_lands_before_detach_keeps_that_home_only(self):
        first, second = self.two()
        self.sweep(0)
        victim = self.key("att-x1")

        def land():
            self.row("att-late", "open", parent_attempt_id="att-x1")

        with self.landing(land, key=victim):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 1)
        self.assertTrue(first.is_dir())
        self.assertFalse(second.exists())
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertNotIn(victim, self.state()["eligible_since"])

    def test_real_writer_publishing_into_its_group_after_discovery_defers(self):
        # The writer's root chmod happens before discovery. Its record lands in
        # the candidate's existing group after discovery, before preparation.
        recipient = "known-owner-parent"
        home = self.add("att-owner", worker_type="owner", parent_sid=recipient,
                        parent_completion_delivery="codex-native-queue")
        group = P.record_directory(self.root, recipient)
        group.mkdir(parents=True)
        self.delivery_record(group / ("delivery-" + "0" * 32 + ".json"), "att-other", "-")
        self.sweep(0)
        path = P.record_path(self.root, recipient, "delivery-" + "7" * 32)
        held = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        parked, errors = threading.Event(), []
        original_flock, original_detach = P.fcntl.flock, R._detach

        def observed(fd, op):
            if threading.current_thread().name == "writer" and op == fcntl.LOCK_EX:
                parked.set()
            return original_flock(fd, op)

        def write():
            receipt = {"fixture": "in-flight"}
            try:
                P.create(self.root, recipient_kind="codex-native-queue", recipient_key=recipient,
                         delivery_id=path.stem, session_generation="",
                         session_generation_supported="0", attempt_ids=["att-owner"],
                         parent_attempt_id="att-owner", route_id="route-free",
                         route_node="supervision", receipt=receipt,
                         receipt_digest=P._canonical_receipt_digest(receipt), row_revisions={})
            except BaseException as exc:  # noqa: BLE001 -- reported below
                errors.append(exc)

        writer = threading.Thread(target=write, name="writer")

        def detach(ctx, *args, **kwargs):
            if writer.is_alive():
                original_flock(held, fcntl.LOCK_UN)
                writer.join(5)
            return original_detach(ctx, *args, **kwargs)

        with mock.patch.object(P.fcntl, "flock", observed), mock.patch.object(R, "_detach", detach):
            writer.start()
            self.assertTrue(parked.wait(5))
            result = self.sweep((8) / 7)
        writer.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(path.read_text())["attempt_ids"], ["att-owner"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())
        self.assertEqual(self.kept(self.sweep((8.1) / 7), "delivery-open"), 1)

    def test_process_quiescence_scan_stays_outside_the_registry_lock(self):
        self.add("att-owner-probe")
        self.sweep(0)
        held, calls = [], []
        original = R._registry_lock
        real = DC.attempt_process_quiescence

        @contextlib.contextmanager
        def lock(jobs):
            with original(jobs) as admitted:
                held.append(True)
                try:
                    yield admitted
                finally:
                    held.pop()

        def probe(meta, *, terminal_receipt=False):
            calls.append(bool(held))
            return real(meta, terminal_receipt=terminal_receipt)

        with mock.patch.object(R, "_registry_lock", lock), \
                mock.patch.object(DC, "attempt_process_quiescence", probe):
            self.assertEqual(self.sweep(8 / 7)["removed"], 1)
        self.assertTrue(calls)
        self.assertNotIn(True, calls)

    def test_provider_admitted_before_final_detach_is_rechecked(self):
        attempt = "att-provider-detach-race"
        home = self.add(attempt)
        _owner_lock, provider_lock = self.attach_summary_owner(attempt, home)
        self.sweep(0)

        original_check = R._late_candidate_delivery_change

        def provider_admission(ctx, candidate, deadline):
            if not provider_lock.exists():
                with AR.admission(self.root, (candidate,), blocking=False) as admitted:
                    if admitted:
                        provider_lock.parent.mkdir(parents=True, exist_ok=True)
                        provider_lock.mkdir()
            return original_check(ctx, candidate, deadline)

        with mock.patch.object(R, "_late_candidate_delivery_change", provider_admission):
            result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "protected:summary-live"), 1)
        self.assertTrue(home.is_dir() and provider_lock.is_dir())

    def _unmapped_candidate(self, attempt):
        home = self.add_unmapped_legacy(attempt)
        os.utime(home, ns=(self.now - int(9 / 7 * RETENTION_NS), self.now - int(9 / 7 * RETENTION_NS)))
        self._optional_sweep(0)
        return home

    def test_unmapped_candidate_late_registry_reference_defers_detach(self):
        attempt = "att-optional-registry-race"
        home = self._unmapped_candidate(attempt)
        with self.landing(lambda: self.row(attempt, "done", runtime_home=str(home)), key=home.name):
            result = self._optional_sweep(8)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1, result)
        self.assertTrue(home.is_dir())

    def test_unmapped_candidate_late_delivery_correction_reference_defers_detach(self):
        attempt = "att-optional-delivery-race"
        home = self._unmapped_candidate(attempt)
        delivery_id = "delivery-" + "b" * 32
        with self.landing(lambda: self.publish_delivery(
                delivery_id, "optional-identity-correction", [attempt], "att-correction-parent"),
                key=home.name):
            result = self._optional_sweep(8)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1, result)
        self.assertTrue(home.is_dir())

    def test_unmapped_candidate_late_replacement_reservation_defers_detach(self):
        attempt = "att-optional-replacement-race"
        home = self._unmapped_candidate(attempt)

        def reserve():
            path = self.root / "automatic-replacements" / "by-source" / f"{attempt}.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema": 1, "original_attempt_id": attempt, "family_id": "a" * 64,
            }))

        with self.landing(reserve, key=home.name):
            result = self._optional_sweep(8)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1, result)
        self.assertTrue(home.is_dir())

    def test_unmapped_candidate_late_held_lease_defers_detach(self):
        attempt = "att-optional-lease-race"
        home = self._unmapped_candidate(attempt)
        lease = DC.supervisor_lease_path(self.jobs, attempt)
        fd_box = []

        def acquire():
            lease.parent.mkdir(parents=True, exist_ok=True)
            lease.write_text("\n".join((
                f"kind={DC.SUPERVISOR_LEASE_KIND}", f"attempt_id={attempt}",
                f"nonce={'a' * 64}", "")))
            fd = os.open(lease, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fd_box.append(fd)

        try:
            with self.landing(acquire, key=home.name):
                result = self._optional_sweep(8)
            self.assertEqual(result["removed"], 0, result)
            self.assertEqual(self.kept(result, "late-evidence-changed"), 1, result)
            self.assertTrue(home.is_dir())
            self.assertEqual(self.kept(self._optional_sweep(8.1), "lease-held"), 1)
        finally:
            for fd in fd_box:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_unmapped_candidate_late_provider_reference_defers_detach(self):
        attempt = "att-optional-provider-race"
        home = self._unmapped_candidate(attempt)
        provider_lock = (Path(os.environ["FLEET_TITLE_STATE_DIR"]) / "claude"
                         / (".lock-dispatch-" + attempt))

        def start_provider():
            provider_lock.parent.mkdir(parents=True, exist_ok=True)
            provider_lock.mkdir()

        with self.landing(start_provider, key=home.name):
            result = self._optional_sweep(8)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1, result)
        self.assertTrue(home.is_dir())
        self.assertEqual(self.unknown(self._optional_sweep(8.1), "summary-owner-unknown"), 1)

    def test_unmapped_candidate_recent_view_wins_before_detach(self):
        home = self._unmapped_candidate("att-optional-view-race")
        view = self.now / 1e9 + 7.5 * 86400
        with self.landing(lambda: R.note_view(self.jobs, str(home), now=view), key=home.name):
            result = self._optional_sweep(8)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "recent-view"), 1, result)
        self.assertTrue(home.is_dir())

    def test_unmapped_candidate_inode_replacement_is_not_detached(self):
        home = self._unmapped_candidate("att-optional-inode-race")
        replacement = self.outside / "replacement-home"
        original_manifest = (home / "worker-home.json").read_bytes()

        def replace_inode():
            os.rename(home, replacement)
            home.mkdir(mode=0o700)
            (home / "worker-home.json").write_bytes(original_manifest)
            (home / "projects").mkdir()
            (home / "projects" / "transcript.jsonl").write_text("replacement")

        with self.landing(replace_inode, key=home.name):
            result = self._optional_sweep(8)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "identity-changed")
                         + self.kept(result, "late-evidence-changed"), 1, result)
        self.assertTrue(home.is_dir() and replacement.is_dir())

    def test_row_reopened_before_detach_keeps_the_home(self):
        home = self.add("att-x3")
        self.sweep(0)
        with self.landing(lambda: self.set_status("att-x3", "running")):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_replacement_reservation_that_lands_while_the_row_is_unchanged(self):
        home = self.add("att-x4")
        self.sweep(0)
        before = self.jobs.read_bytes()

        def land():
            base = self.root / "automatic-replacements" / "by-source"
            base.mkdir(parents=True)
            (base / "att-x4.json").write_text(json.dumps(
                {"schema": 1, "original_attempt_id": "att-x4", "family_id": "f" * 64}))

        with self.landing(land):
            result = self.sweep((8) / 7)
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_pending_delivery_that_lands_while_the_row_is_unchanged(self):
        home = self.add("att-x5")
        self.sweep(0)

        def land():
            folder = self.root / "pending-delivery" / ("c" * 64)
            folder.mkdir(parents=True)
            self.delivery_record(folder / ("delivery-" + "9" * 32 + ".json"),
                                 "att-x5", state="pending")

        with self.landing(land):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_route_that_reopens_before_detach_keeps_the_home(self):
        record = {"schema_version": 1, "capability": "autopilot-code", "nodes": []}
        record["route_hash"] = route_hash(record)
        record["route_id"] = "rt-race"
        path = self.root / "rt-race.json"
        path.write_text(json.dumps(record))
        outcome = self.root / "rt-race.outcome.json"
        outcome.write_text(json.dumps({"route_id": "rt-race", "route_hash": record["route_hash"]}))
        home = self.add("att-x6", route_file=str(path), route_id="rt-race", route_hash=record["route_hash"])
        self.sweep(0)
        with self.landing(outcome.unlink):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_view_marker_that_lands_before_detach_keeps_the_home(self):
        home = self.add("att-x7")
        self.sweep(0)
        wrote = []
        view = self.now / 1e9 + (7.5 / 7 * R.IDLE_SECONDS)
        with self.landing(lambda: wrote.append(R.note_view(self.jobs, str(home), now=view))):
            result = self.sweep((8) / 7)
        self.assertEqual(wrote, [True])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "recent-view"), 1)
        self.assertTrue(home.is_dir())

    def test_late_route_outcome_gets_a_full_new_window(self):
        path, record = Routes.route(self, name="rt-late")
        home = self.add("att-late-route", route_file=str(path), route_id=record["route_id"],
                        route_hash=record["route_hash"])
        self.sweep(0)
        outcome = path.with_name(path.stem + ".outcome.json")
        late = self.now + int(7.5 / 7 * RETENTION_NS)
        with self.landing(lambda: os.utime(outcome, ns=(late, late))):
            self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())

    def test_late_delivery_home_and_supervisor_writes_get_full_windows(self):
        record = self.root / "pending-delivery" / ("c" * 64) / ("delivery-" + "a" * 32 + ".json")
        homes = [self.add("att-late-delivery", delivery_intent="1",
                          delivery_id=record.name[:-5], delivery_recipient_digest=record.parent.name,
                          parent_attempt_id="att-parent"),
                 self.add("att-late-home"), self.add("att-late-supervisor")]
        self.delivery_record(record, "att-late-delivery")
        # A terminal delivery may update without a registry row change.
        state_file = self.root / "supervisor-state" / "att-late-supervisor.json"
        state_file.parent.mkdir()
        state_file.write_text("{}")
        self.sweep(0)
        late = self.now + int(7.5 / 7 * RETENTION_NS)
        def land():
            for path in (record, homes[1], state_file):
                os.utime(path, ns=(late, late))
        with self.landing(land):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(all(home.is_dir() for home in homes))

    def test_in_place_delivery_rewrite_before_detach_defers(self):
        home, record = Deliveries.intent(self, "att-rewrite")
        value = self.delivery_record(record, "att-rewrite")
        self.sweep(0)
        def rewrite():
            with record.open("w") as stream:
                stream.write(json.dumps({**value, "state": "pending"}))
        with self.landing(rewrite):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_historical_delivery_tombstones_do_not_exhaust_freshness(self):
        home = self.add("att-history")
        folder = self.root / "pending-delivery" / ("d" * 64)
        folder.mkdir(parents=True)
        for number in range(R.MAX_DEPENDENCY_STAMPS + 2):
            (folder / f"delivery-{number:032x}.json.pruned").touch()
        self.sweep(0)
        result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())

    def test_new_candidate_tombstone_defers_detach(self):
        home, record = Deliveries.intent(self, "att-late-prune")
        value = self.delivery_record(record, "att-late-prune")
        self.sweep(0)

        def prune():
            record.unlink()
            Path(str(record) + ".pruned").write_text(json.dumps({
                "schema": P.PRUNE_TOMBSTONE_SCHEMA,
                "delivery_id": value["delivery_id"],
                "recipient_digest": value["recipient_digest"],
                "attempt_ids": value["attempt_ids"],
                "parent_attempt_id": value["parent_attempt_id"],
                "terminal_state": "acked",
            }))

        with self.landing(prune):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_other_active_delivery_edit_defers_detach(self):
        # Obsolete expectation retained by name for issue #592 traceability:
        # an unrelated record rewrite no longer defers the due candidate.
        home = self.add("att-unrelated-active")
        record = self.root / "pending-delivery" / ("e" * 64) / ("delivery-" + "f" * 32 + ".json")
        value = self.delivery_record(record, "att-other")
        self.sweep(0)
        with self.landing(lambda: record.write_text(json.dumps({**value, "state": "pending"}))):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())

    def test_parent_only_delivery_reference_landing_keeps_the_home(self):
        recipient = "parent-only-owner-session"
        home = self.add("att-parent-only", worker_type="owner", parent_sid=recipient)
        group = P.record_directory(self.root, recipient)
        self.sweep(0)

        def publish():
            self.delivery_record(group / ("delivery-" + "9" * 32 + ".json"),
                                 "att-other", "att-parent-only", state="pending")

        with self.landing(publish):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_unrelated_record_rewrite_into_candidate_reference_keeps_the_home(self):
        home = self.add("att-rewritten-reference")
        record = self.root / "pending-delivery" / ("e" * 64) / ("delivery-" + "8" * 32 + ".json")
        value = self.delivery_record(record, "att-other", "att-parent", state="acked")
        self.sweep(0)

        def rewrite():
            record.write_text(json.dumps({**value, "attempt_ids": ["att-rewritten-reference"]}))

        with self.landing(rewrite):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_final_boundary_catches_candidate_file_added_to_existing_unrelated_group(self):
        home = self.add("att-final-existing-group")
        group = self.root / "pending-delivery" / ("a" * 64)
        self.delivery_record(group / ("delivery-" + "1" * 32 + ".json"),
                             "att-unrelated", "att-parent", state="acked")
        self.sweep(0)

        broad_done, written = threading.Event(), threading.Event()
        errors = []
        def writer():
            if not broad_done.wait(5):
                errors.append("broad comparison barrier timed out")
                written.set()
                return
            try:
                self.delivery_record(group / ("delivery-" + "2" * 32 + ".json"),
                                     "att-final-existing-group", "att-parent", state="pending")
            except Exception as exc:  # reported on the test thread
                errors.append(repr(exc))
            finally:
                written.set()

        thread = threading.Thread(target=writer)
        thread.start()
        broad = R._late_delivery_change
        def broad_then_barrier(ctx, attempt, deadline):
            result = broad(ctx, attempt, deadline)
            broad_done.set()
            if not written.wait(5):
                raise AssertionError("late delivery writer did not reach the pre-admission barrier")
            return result
        try:
            with mock.patch.object(R, "_late_delivery_change", broad_then_barrier):
                result = self.sweep((8) / 7)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_final_rename_boundary_catches_parent_reference_rewrite_in_known_group(self):
        home = self.add("att-final-parent-rewrite")
        record = self.root / "pending-delivery" / ("b" * 64) / ("delivery-" + "3" * 32 + ".json")
        value = self.delivery_record(record, "att-unrelated", "att-parent", state="acked")
        self.sweep(0)

        admitted, written = threading.Event(), threading.Event()
        errors = []
        def writer():
            if not admitted.wait(5):
                errors.append("final admission barrier timed out")
                written.set()
                return
            try:
                record.write_text(json.dumps({
                    **value, "parent_attempt_id": "att-final-parent-rewrite", "state": "pending",
                }), encoding="utf-8")
            except Exception as exc:  # reported on the test thread
                errors.append(repr(exc))
            finally:
                written.set()

        thread = threading.Thread(target=writer)
        thread.start()
        final_check = R._late_candidate_delivery_change
        def final_barrier(ctx, attempt, deadline):
            admitted.set()
            if not written.wait(5):
                raise AssertionError("parent-reference writer did not reach the pre-rename barrier")
            return final_check(ctx, attempt, deadline)
        try:
            with mock.patch.object(R, "_late_candidate_delivery_change", final_barrier):
                result = self.sweep((8) / 7)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_second_registry_admission_catches_new_candidate_file_in_known_group(self):
        home = self.add("att-final-admission-new-file")
        group = self.root / "pending-delivery" / ("c" * 64)
        self.delivery_record(group / ("delivery-" + "4" * 32 + ".json"),
                             "att-unrelated", "att-parent", state="acked")
        self.sweep(0)
        self._assert_final_admission_write_keeps_home(
            home, lambda: self.delivery_record(
                group / ("delivery-" + "5" * 32 + ".json"),
                "att-final-admission-new-file", "att-parent", state="pending"))

    def test_second_registry_admission_catches_rewritten_candidate_parent_reference(self):
        home = self.add("att-final-admission-parent-rewrite")
        record = self.root / "pending-delivery" / ("d" * 64) / ("delivery-" + "6" * 32 + ".json")
        value = self.delivery_record(record, "att-unrelated", "att-parent", state="acked")
        self.sweep(0)
        self._assert_final_admission_write_keeps_home(home, lambda: record.write_text(json.dumps({
            **value, "parent_attempt_id": "att-final-admission-parent-rewrite", "state": "pending",
        }), encoding="utf-8"))

    def test_second_registry_admission_catches_rewritten_candidate_attempt_reference(self):
        home = self.add("att-final-admission-attempt-rewrite")
        record = self.root / "pending-delivery" / ("e" * 64) / ("delivery-" + "7" * 32 + ".json")
        value = self.delivery_record(record, "att-unrelated", "att-parent", state="acked")
        self.sweep(0)
        self._assert_final_admission_write_keeps_home(home, lambda: record.write_text(json.dumps({
            **value, "attempt_ids": ["att-final-admission-attempt-rewrite"], "state": "pending",
        }), encoding="utf-8"))

    def test_changed_group_record_read_stays_bound_to_scanned_directory(self):
        base = self.root / "pending-delivery"
        group = base / ("a" * 64)
        group.mkdir(parents=True)
        candidate = "att-group-replacement-race"
        delivery_id = "delivery-" + "1" * 32 + ".json"
        original_record = group / delivery_id

        # Capture the checked directory identity before the candidate-related
        # record appears, so this group is inspected as a late change.
        old_ns = time.time_ns() - 10_000_000
        os.utime(group, ns=(old_ns, old_ns))
        before = R._stamp(group)
        self.delivery_record(original_record, candidate)
        baseline = R._CandidateDeliveryBaseline(
            attempt=candidate,
            binding=None,
            related=frozenset({candidate}),
            related_file_stamps=(),
            baseline_files=(),
            baseline_file_paths=frozenset(),
            group_stamps=((group, before),),
            group_names=frozenset({group.name}),
            base=base,
            base_stamp=R._stamp(base),
        )

        displaced = self.root / "displaced-pending-group"
        replacement_attempt = "att-unrelated-group-replacement"
        real_relation = R._delivery_record_relation
        swapped = []

        def replace_group_before_read(path, related, binding=None, **kwargs):
            if not swapped:
                group.rename(displaced)
                group.mkdir()
                self.delivery_record(group / path.name, replacement_attempt)
                swapped.append(True)
            return real_relation(path, related, binding, **kwargs)

        with mock.patch.object(R, "_delivery_record_relation", replace_group_before_read):
            result = R._late_candidate_group_change(
                baseline, baseline.related, baseline.binding,
                baseline.baseline_file_paths, time.monotonic() + 2)

        self.assertEqual(swapped, [True])
        self.assertEqual(result, "changed")
        self.assertEqual(json.loads((displaced / delivery_id).read_text())["attempt_ids"],
                         [candidate])
        self.assertEqual(json.loads((group / delivery_id).read_text())["attempt_ids"],
                         [replacement_attempt])

    def test_late_discovered_group_parent_reference_rewrite_keeps_home(self):
        self._assert_late_discovered_record_rewrite_keeps_home("parent_attempt_id")

    def test_late_discovered_group_attempt_reference_rewrite_keeps_home(self):
        self._assert_late_discovered_record_rewrite_keeps_home("attempt_ids")

    def test_resolver_upgrade_preserves_the_first_identity(self):
        for through_child in (False, True):
            with self.subTest(through_child=through_child):
                parent = self.root / f"resolver-{through_child}"
                other = self.root / f"other-{through_child}"
                parent.mkdir()
                other.mkdir()
                watched = {}
                token = R._DEPENDENCIES.set(watched)
                try:
                    R._track_resolver_parent(parent)
                    parent.rename(self.root / f"old-{through_child}")
                    parent.symlink_to(other, target_is_directory=True)
                    exact = parent / "evidence.json" if through_child else parent
                    with self.assertRaisesRegex(R._Unreadable, "late-evidence-changed"):
                        R._track(exact)
                finally:
                    R._DEPENDENCIES.reset(token)

    def test_late_parent_alias_to_candidate_defers_detach(self):
        home = self.add("att-alias-target")
        alias = self.root / "workers-alias"
        alias.symlink_to(self.outside, target_is_directory=True)
        self.row("att-alias-other", runtime_home=str(alias / home.name))
        self.sweep(0)
        with self.landing(lambda: alias.unlink() or alias.symlink_to(self.workers, target_is_directory=True)):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_inline_finish_marker_change_before_detach_defers(self):
        route = {"schema_version": 1, "capability": "autopilot-code", "nodes": [],
                 "artifact_root": str(self.root)}
        route["route_hash"] = route_hash(route)
        route["route_id"] = "rt-inline-retention"
        path = self.root / "rt-inline-retention.json"
        path.write_text(json.dumps(route))
        path.with_name(path.stem + ".outcome.json").write_text(json.dumps({
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "inline_finish_id": "finish-one"}))
        marker = self.root / ".runtime/inline-finish/v1/rt-inline-retention/finish.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"schema": "inline_finish_v1",
                                      "inline_finish_id": "finish-one", "state": "finished"}))
        home = self.add("att-inline", route_file=str(path), route_id=route["route_id"],
                        route_hash=route["route_hash"])
        self.assertEqual(self.sweep(0)["eligible"], 1)
        with self.landing(lambda: marker.write_text(json.dumps({
                "schema": "inline_finish_v1", "inline_finish_id": "finish-one",
                "state": "node-completed"}))):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue(home.is_dir())

    def test_late_resolved_replacement_claim_gets_a_full_window(self):
        home = self.add("att-late-claim")
        family = "f" * 64
        base = self.root / "automatic-replacements"
        (base / "by-source").mkdir(parents=True)
        (base / "claims").mkdir()
        (base / "by-source" / "att-late-claim.json").write_text(json.dumps(
            {"original_attempt_id": "att-late-claim", "family_id": family}))
        claim = base / "claims" / f"{family}.json"
        claim.write_text(json.dumps({"original_attempt_id": "att-late-claim",
                                     "replacement_attempt_id": "att-successor"}))
        self.row("att-successor", "done")
        self.sweep(0)
        late = self.now + int(7.5 / 7 * RETENTION_NS)
        with self.landing(lambda: os.utime(claim, ns=(late, late))):
            self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())

    def test_a_view_cannot_pass_a_detach_in_progress(self):
        home = self.add("att-x8")
        self.sweep(0)
        blocked = []
        real = os.rename

        def observe(src, dst, **kwargs):
            # The final detach holds the reader-mark lock exclusively right here.
            probe = os.open(self.root / "homes/worker-retention/view.lock", os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                blocked.append(False)
            except BlockingIOError:
                blocked.append(True)
            finally:
                os.close(probe)
            return real(src, dst, **kwargs)

        with self.replaced("rename", observe):
            result = self.sweep((8) / 7)
        self.assertEqual(blocked, [True])
        self.assertEqual(result["removed"], 1)
        # After the rename the home is gone, so a late view writes nothing.
        self.assertFalse(R.note_view(self.jobs, str(home), now=self.now / 1e9 + (8 / 7 * R.IDLE_SECONDS)))
        self.assertFalse((self.root / "homes/worker-retention/views" / self.key("att-x8")).exists())

    def test_swapped_home_is_not_detached(self):
        home = self.add("att-x9")
        self.sweep(0)

        def swap():
            displaced = self.outside / "displaced"
            home.rename(displaced)
            home.mkdir()
            (home / "worker-home.json").write_bytes((displaced / "worker-home.json").read_bytes())
            (home / "planted").write_text("different directory")

        with self.landing(swap):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertEqual((home / "planted").read_text(), "different directory")


class Containment(Fixture):
    def test_alias_registry_reopen_and_lock_use_canonical_identity(self):
        home = self.add("att-canonical")
        alias = self.root / "jobs-alias.log"
        alias.symlink_to(self.jobs)
        self.sweep(0)
        with self.landing(lambda: self.set_status("att-canonical", "running")):
            result = R.sweep(alias, now_ns=self.now + int(8 / 7 * RETENTION_NS), force=True)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(home.is_dir())
        self.set_status("att-canonical", "done")
        completion = datetime.fromtimestamp(
            (self.now + int(8.1 / 7 * RETENTION_NS)) / 10**9, timezone.utc
        ).isoformat().replace("+00:00", "Z")
        self.set_timestamp("att-canonical", completion)
        R.sweep(alias, now_ns=self.now + int(8.1 / 7 * RETENTION_NS), force=True)
        fd = os.open(str(self.jobs) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            result = R.sweep(alias, now_ns=self.now + int(17 / 7 * RETENTION_NS), force=True)
            self.assertEqual(result["kept"].get("registry-lock-busy"), 1)
            self.assertTrue(home.is_dir())
        finally:
            os.close(fd)

    def test_registry_resolution_failures_stay_best_effort(self):
        loop = self.root / "jobs-loop"
        loop.symlink_to(loop)
        # Python 3.12 raises on a symlink loop; 3.13 leaves it unresolved.
        # Either way nothing escapes and nothing is removed.
        self.assertEqual(R.sweep(loop, force=True).get("removed", 0), 0)
        self.assertFalse(R.note_view(loop, str(self.root / "homes/codex/shared")))
        with mock.patch.object(Path, "resolve", side_effect=OSError("resolution failed")):
            self.assertIn("skipped", R.sweep(self.jobs, force=True))
            self.assertFalse(R.note_view(self.jobs, str(self.workers / ("a" * 32))))

    def test_missing_mount_identity_before_detach_leaves_home_and_clock(self):
        for part in ("workers", "retiring", "leaf"):
            with self.subTest(part=part):
                home = self.add("att-mount-pre-" + part)
                self.sweep(0)
                first = self.state()["eligible_since"][home.name]
                actual = R._mount_id
                def mount_id(fd):
                    path = os.readlink(f"/proc/self/fd/{fd}")
                    if (part == "leaf" and path == str(home)) or path.endswith("/" + part):
                        return None
                    return actual(fd)
                with mock.patch.object(R, "_mount_id", side_effect=mount_id):
                    result = self.sweep((8) / 7)
                self.assertEqual(result["removed"], 0)
                self.assertGreaterEqual(result["kept"].get("mount-unproven", 0), 1)
                self.assertTrue(home.is_dir())
                self.assertEqual(self.state()["eligible_since"][home.name], first)
                self.assertEqual(list((self.root / "homes/worker-retention/retiring").iterdir()), [])
                self.sweep((8.1) / 7)

    def test_links_inside_a_home_are_never_followed(self):
        target = self.outside / "release"
        target.mkdir()
        (target / "important").write_text("keep")
        credential = self.outside / "credentials.json"
        credential.write_text("{}")
        home = self.add("att-c1")
        (home / ".credentials.json").symlink_to(credential)
        (home / "hearting").symlink_to(target)
        runtime = home / "runtime" / "config"
        runtime.mkdir(parents=True)
        (runtime / "opencode").symlink_to(home)
        nested = home / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "up").symlink_to(self.outside)
        (nested / "dangling").symlink_to(self.outside / "nowhere")
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())
        self.assertEqual((target / "important").read_text(), "keep")
        self.assertEqual(credential.read_text(), "{}")
        self.assertTrue(self.outside.is_dir())

    def test_symlinked_workers_directory_is_refused(self):
        self.add("att-c2")
        elsewhere = self.outside / "workers"
        self.workers.rename(elsewhere)
        self.workers.symlink_to(elsewhere)
        result = self.sweep(0)
        self.assertEqual(result, {"skipped": "containment"})
        self.assertEqual(self.sweep((8) / 7), {"skipped": "containment"})
        self.assertTrue((elsewhere / self.key("att-c2")).is_dir())

    def test_symlink_leaf_and_foreign_entries_are_ignored(self):
        victim = self.outside / "victim"
        victim.mkdir()
        (victim / "worker-home.json").write_text("{}")
        (self.workers / ("1" * 32)).symlink_to(victim)
        (self.workers / "not-a-key").mkdir()
        (self.workers / ("2" * 32)).write_text("file")
        result = self.expire()
        self.assertEqual(result["ignored"], 3)
        self.assertTrue((victim / "worker-home.json").is_file())

    def test_planted_directory_under_retiring_is_not_deleted(self):
        retiring = self.root / "homes/worker-retention/retiring"
        retiring.mkdir(parents=True, mode=0o700)
        planted = retiring / (self.key("att-c3") + ".123")
        planted.mkdir()
        (planted / "mine").write_text("not a retired home")
        forged = retiring / (self.key("att-c4") + ".456")
        forged.mkdir()
        (forged / "mine").write_text("journal names another inode")
        (retiring / (forged.name + ".journal")).write_text(json.dumps(
            {"schema": 1, "key": self.key("att-c4"), "name": forged.name, "identity": [1, 2, os.geteuid()]}))
        linked = retiring / (self.key("att-c5") + ".789")
        (self.outside / "keep").mkdir()
        (self.outside / "keep" / "file").write_text("x")
        linked.symlink_to(self.outside / "keep")
        st = os.stat(self.outside / "keep")
        (retiring / (linked.name + ".journal")).write_text(json.dumps(
            {"schema": 1, "key": self.key("att-c5"), "name": linked.name,
             "identity": [st.st_dev, st.st_ino, st.st_uid]}))
        result = self.sweep(0)
        self.assertEqual(result["unproven_leftover"], 3)
        self.assertEqual(result["detached_leftover"], 0)
        self.assertEqual((planted / "mine").read_text(), "not a retired home")
        self.assertEqual((forged / "mine").read_text(), "journal names another inode")
        self.assertEqual((self.outside / "keep" / "file").read_text(), "x")

    def test_parent_substitution_cannot_redirect_the_detach_or_removal(self):
        self.add("att-c6")
        self.sweep(0)

        def substitute():
            displaced = self.root / "homes" / "workers-displaced"
            self.workers.rename(displaced)
            decoy = self.root / "homes" / "workers"
            decoy.mkdir()
            planted = decoy / self.key("att-c6")
            planted.mkdir()
            (planted / "worker-home.json").write_text("{}")
            (planted / "precious").write_text("planted")

        with self.landing(substitute):
            result = self.sweep((8) / 7)
        # A changed parent defers; neither the pinned original nor the decoy is removed.
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.kept(result, "late-evidence-changed"), 1)
        self.assertTrue((self.root / "homes" / "workers-displaced" / self.key("att-c6")).exists())
        self.assertEqual((self.workers / self.key("att-c6") / "precious").read_text(), "planted")

    def test_removal_never_crosses_onto_another_device(self):
        home = self.add("att-c7")
        (home / "mount").mkdir()
        (home / "mount" / "inner").write_text("data")
        self.sweep(0)
        real = os.stat
        main_dev = real(home).st_dev

        def stat_other_device(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path == "mount" and kwargs.get("dir_fd") is not None:
                values = list(result)
                values[2] = main_dev + 1
                return os.stat_result(values)
            return result

        with self.replaced("stat", stat_other_device):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["partial"], 1)
        leaf = next((self.root / "homes/worker-retention/retiring").glob("*.*[0-9]"))
        self.assertTrue((leaf / "mount" / "inner").is_file())

    def test_same_device_mount_identity_or_unknown_refuses_descent(self):
        for invalid in ("different", None):
            with self.subTest(invalid=invalid):
                home = self.add("att-mount-" + str(invalid))
                (home / "mount").mkdir()
                (home / "mount" / "inner").write_text("keep")
                self.sweep(0)
                actual = R._mount_id
                def mount_id(fd):
                    # The descriptor is the actual child; the device stays the same.
                    return invalid if os.readlink(f"/proc/self/fd/{fd}").endswith("/mount") else actual(fd)
                with mock.patch.object(R, "_mount_id", side_effect=mount_id):
                    result = self.sweep((8) / 7)
                self.assertEqual(result["removed"], 0)
                self.assertEqual(result["partial"], 1)
                leaf = next((self.root / "homes/worker-retention/retiring").glob("*.*[0-9]"))
                self.assertEqual((leaf / "mount" / "inner").read_text(), "keep")
                # Clear the interrupted leaf through the normal resumed path.
                self.sweep((16) / 7)

    def test_unsupported_platform_detaches_nothing(self):
        home = self.add("att-c8")
        with mock.patch.object(R, "_symlink_safe_removal_supported", return_value=False):
            self.assertEqual(self.sweep(0), {"skipped": "removal-unsupported"})
            self.assertEqual(self.sweep((8) / 7), {"skipped": "removal-unsupported"})
        self.assertTrue(home.is_dir())


class Orphans(Fixture):
    def _remove_registry_rows(self, *attempts):
        for attempt in attempts:
            self.rows.pop(attempt, None)
        self.write_rows()

    def test_mixed_harness_no_row_homes_retire_when_existing_evidence_is_clear(self):
        homes = []
        for index, harness in enumerate(("claude", "codex", "opencode")):
            attempt = f"att-orphan-{index}"
            homes.append(self.add(attempt, harness=harness))
            self._remove_registry_rows(attempt)
        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                mock.patch.object(R._Context, "home_process_evidence",
                                  return_value=("absent", "fixture-process-scan-complete")):
            result = self.expire()
        self.assertEqual(result["removed"], 3)
        self.assertTrue(all(not home.exists() for home in homes))

    def test_unmapped_legacy_absence_live_use_and_unknown_observation_across_harnesses(self):
        homes = {"absent": {}, "live": {}, "unknown": {}}
        child_specs = []
        unknown_keys = set()
        old = self.now - int(9 / 7 * RETENTION_NS)
        for harness in ("claude", "codex", "opencode"):
            attempt = f"att-unmapped-{harness}"
            homes["absent"][harness] = self.add_unmapped_legacy(
                attempt + "-absent", harness=harness)
            homes["live"][harness] = self.add_unmapped_legacy(
                attempt + "-live", harness=harness)
            homes["unknown"][harness] = self.add_unmapped_legacy(
                attempt + "-unknown", harness=harness)
            unknown_keys.add(homes["unknown"][harness].name)
        for group in homes.values():
            for home in group.values():
                os.utime(home, ns=(old, old))

        live_cases = (
            ("claude", "env"), ("codex", "cwd"), ("opencode", "fd"),
        )
        children = []
        try:
            for harness, mode in live_cases:
                home = homes["live"][harness]
                if mode == "env":
                    child_specs.append(subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        env={**os.environ, "HEARTING_WORKER_HOME": str(home)}))
                elif mode == "cwd":
                    child_specs.append(subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        cwd=home / "projects"))
                else:
                    transcript = home / "projects" / "transcript.jsonl"
                    fd = os.open(transcript, os.O_RDONLY)
                    os.set_inheritable(fd, True)
                    try:
                        child_specs.append(subprocess.Popen(
                            [sys.executable, "-c",
                             "import os,sys,time; f=os.fdopen(int(sys.argv[1])); time.sleep(30)" ,
                             str(fd)], pass_fds=(fd,)))
                    finally:
                        os.close(fd)
            children = child_specs

            original = R._Context.home_process_evidence

            def process_evidence(ctx, key):
                if key in unknown_keys:
                    return "unknown", "fixture-process-scan-incomplete"
                return original(ctx, key)

            # These private HOME paths can only be referenced by this test process
            # and the children just launched. Enumerate that complete cohort, then
            # let the production scanner read each process's real env/cwd/fds.
            with self.process_inventory(os.getpid(), *(child.pid for child in children)), \
                    mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                    mock.patch.object(R._Context, "home_process_evidence", process_evidence):
                first = self.sweep(0)
                self.assertEqual(first["eligible"], 3, first)
                self.assertEqual(self.kept(first, "not-quiescent"), 3, first)
                self.assertEqual(self.unknown(first, "quiescence-unverifiable"), 3, first)
                self.assertEqual(
                    first["unknown_causes"]["quiescence-unverifiable"],
                    {"fixture-process-scan-incomplete": 3},
                )
                result = self.sweep(8 / 7)

            self.assertEqual(result["removed"], 3, result)
            self.assertTrue(all(not home.exists() for home in homes["absent"].values()))
            self.assertTrue(all(home.is_dir() for home in homes["live"].values()))
            self.assertTrue(all(home.is_dir() for home in homes["unknown"].values()))
            self.assertEqual(self.kept(result, "not-quiescent"), 3, result)
            self.assertEqual(self.unknown(result, "quiescence-unverifiable"), 3, result)
            self.assertEqual(
                result["unknown_causes"]["quiescence-unverifiable"],
                {"fixture-process-scan-incomplete": 3},
            )
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)

    def test_unmapped_legacy_unreadable_process_metadata_stays_unknown_with_actual_cause(self):
        home = self.add_unmapped_legacy("att-unmapped-unreadable-process")
        with self.process_inventory(os.getpid(), unreadable_environ=(os.getpid(),)), \
                mock.patch.object(DC, "_current_observer_is_host_like", return_value=True):
            result = self.expire()

        self.assertTrue(home.is_dir())
        self.assertEqual(self.unknown(result, "quiescence-unverifiable"), 1, result)
        self.assertEqual(
            result["unknown_causes"]["quiescence-unverifiable"],
            {"process-reference-unreadable": 1},
        )

    def test_reader_pin_is_per_home_and_does_not_block_unrelated_detach(self):
        reader = self.add_unmapped_legacy("att-unmapped-reader-pin")
        other = self.add_unmapped_legacy("att-unmapped-other-home")
        old = self.now - int(9 / 7 * RETENTION_NS)
        for home in (reader, other):
            os.utime(home, ns=(old, old))
        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                mock.patch.object(R._Context, "home_process_evidence",
                                  return_value=("absent", "fixture-process-scan-complete")):
            with R.worker_home_read_admission(
                    self.jobs, reader, None, "att-unmapped-reader-pin", "claude") as admitted:
                self.assertTrue(admitted)
                self.sweep(0)
                result = self.sweep(8 / 7)
                self.assertEqual(result["removed"], 1, result)
                self.assertEqual(self.kept(result, "protected:home-reader-active"), 1, result)
                self.assertTrue(reader.is_dir())
                self.assertFalse(other.exists())
                with R.worker_home_read_admission(
                        self.jobs, other, None, "att-unmapped-other-home", "claude") as late:
                    self.assertFalse(late)

    def test_unmapped_legacy_reader_and_detach_first_share_path_admission(self):
        attempt = "att-unmapped-reader-race"
        home = self.add_unmapped_legacy(attempt)
        old = self.now - int(9 / 7 * RETENTION_NS)
        os.utime(home, ns=(old, old))
        transcript = home / "projects" / "transcript.jsonl"

        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                mock.patch.object(R._Context, "home_process_evidence",
                                  return_value=("absent", "fixture-process-scan-complete")):
            with R.worker_home_read_admission(
                    self.jobs, home, transcript, attempt, "claude") as admitted:
                self.assertTrue(admitted)
                self.sweep(0)
                result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "protected:home-reader-active"), 1, result)
        self.assertTrue(home.is_dir())

        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                mock.patch.object(R._Context, "home_process_evidence",
                                  return_value=("absent", "fixture-process-scan-complete")):
            # A live reader restarts the eligibility clock; detach wins only
            # after the reader released and a fresh full window elapsed.
            self.assertEqual(self.sweep(8.1 / 7)["removed"], 0)
            result = self.sweep(16.2 / 7)
            self.assertEqual(result["removed"], 1, result)
            with R.worker_home_read_admission(
                    self.jobs, home, transcript, attempt, "claude") as admitted:
                self.assertFalse(admitted)

    def test_identityless_legacy_home_maps_through_retained_delivery_attempt(self):
        attempt = "att-orphan-legacy"
        home = self.add(attempt, schema=1)
        self._remove_registry_rows(attempt)
        group = self.root / "pending-delivery" / ("d" * 64)
        record = group / ("delivery-" + "e" * 32 + ".json")
        self.delivery_record(record, attempt, state="acked")
        with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                mock.patch.object(R._Context, "home_process_evidence",
                                  return_value=("absent", "fixture-process-scan-complete")):
            result = self.expire()
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())

    def test_no_row_open_delivery_and_exact_supervisor_lease_keep_home(self):
        delivery_attempt = "att-orphan-delivery"
        delivery_home = self.add(delivery_attempt)
        self._remove_registry_rows(delivery_attempt)
        group = self.root / "pending-delivery" / ("f" * 64)
        delivery = group / ("delivery-" + "a" * 32 + ".json")
        self.delivery_record(delivery, delivery_attempt, state="pending")

        lease_attempt = "att-orphan-lease"
        lease_home = self.add(lease_attempt)
        self._remove_registry_rows(lease_attempt)
        lease = DC.supervisor_lease_path(self.jobs, lease_attempt)
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text(chr(10).join((f"kind={DC.SUPERVISOR_LEASE_KIND}",
                                      f"attempt_id={lease_attempt}",
                                      f"nonce={'a' * 64}", "")))
        fd = os.open(lease, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True):
                result = self.expire()
            self.assertEqual(self.kept(result, "delivery-open"), 1)
            self.assertEqual(self.kept(result, "lease-held"), 1)
            self.assertTrue(delivery_home.is_dir() and lease_home.is_dir())
        finally:
            os.close(fd)

    def test_process_environment_cwd_and_open_fd_keep_no_row_homes(self):
        attempts = ("att-orphan-env", "att-orphan-cwd", "att-orphan-fd")
        homes = [self.add(attempt) for attempt in attempts]
        self._remove_registry_rows(*attempts)
        children = []
        try:
            children.append(subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env={**os.environ, "HEARTING_WORKER_HOME": str(homes[0])}))
            children.append(subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], cwd=homes[1]))
            transcript = homes[2] / "projects" / "transcript.jsonl"
            fd = os.open(transcript, os.O_RDONLY)
            os.set_inheritable(fd, True)
            try:
                children.append(subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(fd,)))
            finally:
                os.close(fd)
            with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True):
                result = self.expire()
            self.assertEqual(self.kept(result, "not-quiescent"), 3)
            self.assertTrue(all(home.is_dir() for home in homes))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()

    def test_process_scan_resolves_handed_paths_once_per_pass_and_link_targets_never(self):
        attempts = ("att-orphan-scan-1", "att-orphan-scan-2", "att-orphan-scan-3")
        homes = [self.add(attempt) for attempt in attempts]
        self._remove_registry_rows(*attempts)
        link = self.outside / "home-link"
        link.symlink_to(homes[0])
        held = self.outside / "held"
        held.mkdir()
        fds = [os.open(held / str(index), os.O_RDONLY | os.O_CREAT, 0o600) for index in range(64)]
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env={**os.environ, "HEARTING_WORKER_HOME": str(link)}, pass_fds=fds)
        finally:
            for fd in fds:
                os.close(fd)
        realpath = os.path.realpath
        resolved = []

        def counted(path, *args, **kwargs):
            resolved.append(os.fspath(path))
            return realpath(path, *args, **kwargs)

        try:
            with mock.patch.object(DC, "_current_observer_is_host_like", return_value=True), \
                    mock.patch.object(os.path, "realpath", counted):
                result = self.sweep(0)
            # The symlinked HOME is still found, the link is resolved once for three
            # homes, and the child's open files are compared as the kernel names them.
            self.assertEqual(self.kept(result, "not-quiescent"), 1)
            self.assertEqual(resolved.count(str(link)), 1)
            self.assertEqual([path for path in resolved if path.startswith(str(held) + os.sep)], [])
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


class Mapping(Fixture):
    def test_schema_two_manifest_binds_without_a_runtime_home_row(self):
        home = self.add("att-m1", declare=False)
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_legacy_manifest_binds_by_runtime_home_or_unique_key(self):
        declared = self.add("att-m2", schema=1)
        derived = self.add("att-m3", schema=1, declare=False)
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 2)
        self.assertFalse(declared.exists() or derived.exists())

    def test_runtime_home_that_only_shares_a_basename_is_a_conflict(self):
        home = self.add("att-m4", declare=False)
        self.rows["att-m4"][1]["runtime_home"] = str(self.outside / self.key("att-m4"))
        self.write_rows()
        result = self.expire()
        self.assertEqual(self.unknown(result, "runtime-home-conflict"), 1)
        self.assertTrue(home.is_dir())

    def test_runtime_home_through_a_planted_link_is_not_followed(self):
        home = self.add("att-m5", declare=False)
        link = self.outside / "workers-link"
        link.symlink_to(self.workers)
        self.rows["att-m5"][1]["runtime_home"] = str(link / self.key("att-m5"))
        self.write_rows()
        # Only the parent resolves, and it resolves to the real workers directory.
        self.sweep(0)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_manifest_and_row_disagreement_is_unknown(self):
        first = self.add("att-m6")
        second = self.add("att-m7")
        self.rows["att-m7"][1]["runtime_home"] = str(first)
        self.write_rows()
        result = self.expire()
        self.assertTrue(first.is_dir() and second.is_dir())
        self.assertGreaterEqual(sum(result["unknown"].values()), 2)

    def test_shared_destination_and_unmapped_homes_are_kept(self):
        destination = self.add("att-m8", destination=True)
        unmapped = self.add("att-m9", schema=1, home=True)
        self.rows.pop("att-m9")
        self.write_rows()
        broken = self.add("att-m10")
        (broken / "worker-home.json").write_text("{")
        oversized = self.add("att-m11")
        (oversized / "worker-home.json").write_text(" " * (R.MAX_MANIFEST_BYTES + 1))
        linked = self.add("att-m12")
        (linked / "worker-home.json").unlink()
        (linked / "worker-home.json").symlink_to(self.outside / "manifest")
        # Model a complete bounded inventory whose observer cannot establish
        # namespace visibility. The production path supplies the real cause.
        with self.process_inventory(os.getpid()), \
                mock.patch.object(DC, "_current_observer_is_host_like", return_value=False):
            result = self.expire()
        for path in (destination, unmapped, broken, oversized, linked):
            self.assertTrue(path.is_dir())
        self.assertEqual(self.unknown(result, "destination-home"), 1)
        self.assertEqual(self.unknown(result, "quiescence-unverifiable"), 1)
        self.assertEqual(
            result["unknown_causes"]["quiescence-unverifiable"].get(
                "process-namespace-unverifiable", 0),
            1,
        )
        self.assertEqual(self.unknown(result, "manifest-unreadable"), 3)

    def test_two_attempts_naming_one_home_are_ambiguous(self):
        home = self.add("att-m13")
        self.row("att-m14", runtime_home=str(home))
        result = self.expire()
        self.assertEqual(self.unknown(result, "runtime-home-ambiguous"), 1)
        self.assertTrue(home.is_dir())

    def test_an_attempt_id_that_is_not_a_plain_identifier_is_never_bound(self):
        attempt = "../../escape"
        key = self.key(attempt)
        home = self.workers / key
        home.mkdir()
        (home / "worker-home.json").write_text(json.dumps({
            "harness": "claude", "worker_type": "stage", "source": SOURCE, "profile": None}))
        self.row(attempt, "done", runtime_home=str(home))
        result = self.expire()
        self.assertEqual(self.unknown(result, "attempt-invalid"), 1)
        self.assertTrue(home.is_dir())

    def test_codex_and_profile_homes_are_never_enumerated(self):
        shared = self.root / "homes" / "codex" / ("a" * 32)
        shared.mkdir(parents=True)
        (shared / "worker-home.json").write_text("{}")
        profile = self.root / "homes" / "slug.code-report"
        profile.mkdir()
        (profile / "worker-home.json").write_text("{}")
        old = (self.now - int(100 / 7 * RETENTION_NS)) / 1e9
        for path in (shared, profile):
            os.utime(path, (old, old))
        result = self.expire((30) / 7)
        self.assertEqual(result["examined"], 0)
        self.assertTrue(shared.is_dir() and profile.is_dir())


class Bounds(Fixture):
    def test_retention_policy_uses_one_hour_and_keeps_time_budgets(self):
        self.assertEqual(R.IDLE_SECONDS, 3600)
        self.assertEqual(R.VIEW_REFRESH_SECONDS, 300)
        self.assertLessEqual(R.VIEW_REFRESH_SECONDS, R.IDLE_SECONDS / 12)
        self.assertEqual(R.REMOVE_LIMIT, 16)
        self.assertEqual(R.EXAMINE_LIMIT, 128)
        self.assertEqual(R.EXAMINE_BUDGET_SECONDS, 10)
        self.assertEqual(R.REMOVE_DEADLINE_SECONDS, 20)

    def test_operational_scale_pass_makes_progress_within_removal_deadline(self):
        history_count = 5308
        home_count = 340
        due_count = 40
        protected_count = home_count - due_count
        attempts = [f"att-scale-{index:05d}" for index in range(history_count)]
        home_attempts = sorted(attempts[:home_count], key=self.key)
        due_attempts = set(home_attempts[:due_count])
        protected_attempts = set(home_attempts[due_count:])
        old_ns = self.now - 2 * RETENTION_NS
        old_stamp = datetime.fromtimestamp(old_ns / 10**9, timezone.utc).isoformat().replace("+00:00", "Z")
        current_stamp = datetime.fromtimestamp(self.now / 10**9, timezone.utc).isoformat().replace("+00:00", "Z")

        for attempt in attempts:
            is_home = attempt in due_attempts or attempt in protected_attempts
            status = "running" if attempt in protected_attempts else "done"
            meta = {"attempt_id": attempt, "harness": "claude", "worker_type": "stage",
                    "launch_outcome": "never-launched", "padding": "x" * 5000,
                    "main_runtime_harness": "claude", "main_runtime_pid": "2147483647",
                    "main_runtime_pid_start": "1", "main_runtime_ns": DC.process_namespace_identity()}
            if is_home:
                meta["runtime_home"] = str(self.home_path(attempt))
            self.rows[attempt] = (status, meta)
            self.timestamps[attempt] = old_stamp if attempt in due_attempts else current_stamp
        self.write_rows()
        registry_bytes = self.jobs.stat().st_size
        self.assertGreaterEqual(registry_bytes, 24 * 1024 * 1024)
        self.assertLess(registry_bytes, 28 * 1024 * 1024)

        for attempt in sorted(due_attempts | protected_attempts, key=self.key):
            home = self.home_path(attempt)
            home.mkdir(mode=0o700)
            key = home.name
            manifest = {"harness": "claude", "worker_type": "stage", "source": SOURCE,
                        "profile": None, "schema": 2, "identity": attempt,
                        "key": key, "destination": False}
            (home / "worker-home.json").write_text(json.dumps(manifest))
            (home / "projects").mkdir()
            (home / "projects" / "transcript.jsonl").write_text("x" * 100)
            stamp_ns = old_ns if attempt in due_attempts else self.now
            os.utime(home, ns=(stamp_ns, stamp_ns))

        delivery_group = self.root / "pending-delivery" / ("c" * 64)
        for number in range(2000):
            self.delivery_record(delivery_group / f"delivery-{number:032x}.json",
                                 f"att-unrelated-{number}", state="acked")

        detach_times = []
        original_detach = R._detach

        def measured_detach(*args, **kwargs):
            deadline = kwargs.get("deadline", args[6] if len(args) > 6 else None)
            outcome = original_detach(*args, **kwargs)
            if outcome.startswith("detached:"):
                detach_times.append((time.monotonic(), deadline))
            return outcome

        sweep_now = self.now + int(8 / 7 * RETENTION_NS)
        started = time.monotonic()
        with mock.patch.object(R, "_detach", side_effect=measured_detach):
            result = R.sweep(self.jobs, now_ns=sweep_now, force=True)
        elapsed = time.monotonic() - started
        within_deadline = sum(finished <= deadline for finished, deadline in detach_times)
        self.assertGreaterEqual(result["due"], due_count)
        self.assertLessEqual(result["examined"], R.EXAMINE_LIMIT)
        self.assertLessEqual(result["removed"] + result["partial"], R.REMOVE_LIMIT)
        self.assertGreaterEqual(within_deadline, 6, result)
        self.assertEqual(sum(not self.home_path(attempt).exists() for attempt in due_attempts),
                         result["removed"])
        self.assertTrue(all(self.home_path(attempt).is_dir() for attempt in protected_attempts))
        print(json.dumps({"registry_mib": round(registry_bytes / 1024**2, 2),
                          "history_rows": history_count, "delivery_records": 2000,
                          "examined": result["examined"], "due": result["due"],
                          "removed": result["removed"], "partial": result["partial"],
                          "detached_within_deadline": within_deadline,
                          "elapsed_s": round(elapsed, 3)}))

    def test_real_history_over_32768_stays_bounded_per_candidate(self):
        count = 32769
        for kind in ("bindings", "advances", "recipients", "combined"):
            with self.subTest(kind=kind):
                fixture = Fixture()
                fixture.setUp()
                try:
                    base = fixture.root
                    if kind in ("bindings", "combined"):
                        folder = base / "owner-route-bindings"
                        folder.mkdir()
                        for number in range(count):
                            (folder / f"{number:064x}.json").write_text("{}")
                    if kind in ("advances", "combined"):
                        folder = base / "owner-route-advances"
                        folder.mkdir()
                        for number in range(count):
                            (folder / f"{number:064x}").mkdir()
                    if kind in ("recipients", "combined"):
                        folder = base / "pending-delivery"
                        folder.mkdir()
                        for number in range(count):
                            group = folder / f"{number:064x}"
                            group.mkdir()
                            (group / ("delivery-" + "0" * 32 + ".json.pruned")).touch()
                    homes = [fixture.add(f"att-scale-{kind}-{n}") for n in range(3)]
                    fixture.sweep(0)
                    discovery, preparation, dependencies, holds = [], [], [], []
                    original_index, original_detach = R._delivery_index, R._detach
                    original_fresh, original_lock = R._fresh, R._registry_lock
                    def indexed(*args, **kwargs):
                        start = time.monotonic()
                        try:
                            return original_index(*args, **kwargs)
                        finally:
                            discovery.append(time.monotonic() - start)
                    def detached(*args, **kwargs):
                        start = time.monotonic()
                        try:
                            return original_detach(*args, **kwargs)
                        finally:
                            preparation.append(time.monotonic() - start)
                    def fresh(watched):
                        dependencies.append(len(watched))
                        return original_fresh(watched)
                    @contextlib.contextmanager
                    def locked(jobs):
                        with original_lock(jobs) as admitted:
                            start = time.monotonic()
                            try:
                                yield admitted
                            finally:
                                holds.append(time.monotonic() - start)
                    with mock.patch.object(R, "_delivery_index", indexed), \
                            mock.patch.object(R, "_detach", detached), \
                            mock.patch.object(R, "_fresh", fresh), \
                            mock.patch.object(R, "_registry_lock", locked):
                        result = fixture.sweep((8) / 7)
                    self.assertEqual(result["removed"], 3, (kind, result))
                    self.assertTrue(all(not home.exists() for home in homes))
                    self.assertEqual(len(preparation), 3)
                    self.assertEqual(len(holds), 6)
                    self.assertLess(max(dependencies), 512)
                    self.assertLess(max(holds), 0.25)
                    self.assertLess(max(preparation), 3.0)
                    print(json.dumps({"history": kind, "entries_per_history": count,
                                      "discovery_s": discovery, "preparation_s": preparation,
                                      "dependency_count_max": max(dependencies),
                                      "registry_hold_s": holds}))
                finally:
                    fixture.doCleanups()

    def test_default_removal_limit_applies_then_the_rest(self):
        homes = [self.add(f"att-b{index}") for index in range(R.REMOVE_LIMIT + 3)]
        self.sweep(0)
        first = self.sweep(8 / 7)
        self.assertEqual(first["removed"], R.REMOVE_LIMIT)
        self.assertEqual(self.kept(first, "deferred"), 3)
        second = self.sweep(8.1 / 7)
        self.assertEqual(second["removed"], 3)
        self.assertFalse(any(path.exists() for path in homes))
        self.assertEqual(self.sweep(8.2 / 7)["removed"], 0)

    def test_fair_cursor_reaches_eligible_homes_behind_protected_ones(self):
        attempts = [f"att-f{index}" for index in range(R.EXAMINE_LIMIT + 5)]
        ordered = sorted(attempts, key=self.key)
        protected, eligible = ordered[:R.EXAMINE_LIMIT], ordered[R.EXAMINE_LIMIT:]
        for attempt in protected:
            self.add(attempt, "running")
        paths = [self.add(attempt) for attempt in eligible]
        self.sweep(0)
        self.sweep((0.001) / 7)
        for step in range(1, 8):
            self.sweep((8 + step / 1000) / 7)
            if not any(path.exists() for path in paths):
                break
        self.assertFalse(any(path.exists() for path in paths))
        self.assertTrue(all(self.home_path(attempt).is_dir() for attempt in protected))

    def test_examine_limit_applies_and_cursor_advances(self):
        for index in range(R.EXAMINE_LIMIT + 5):
            self.add(f"att-e{index}", "running")
        first = self.sweep(0)
        second = self.sweep((0.001) / 7)
        self.assertEqual(first["examined"], R.EXAMINE_LIMIT)
        self.assertEqual(second["examined"], R.EXAMINE_LIMIT)
        self.assertNotEqual(self.state()["cursor"], "")

    def test_due_interval_and_busy_lock(self):
        self.add("att-i1")
        R.sweep(self.jobs, now_ns=self.now)
        self.assertEqual(R.sweep(self.jobs, now_ns=self.now + 10**9), {"skipped": "not-due"})
        self.assertNotIn("skipped", R.sweep(self.jobs, now_ns=self.now + 301 * 10**9))
        fd = os.open(self.root / "homes/worker-retention/lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.sweep((1) / 7), {"skipped": "busy"})
        finally:
            os.close(fd)

    def test_examine_time_budget_stops_before_the_candidate_limit(self):
        for index in range(5):
            self.add(f"att-examine-budget-{index}", "running")
        clock = [0.0]
        real_examine = R._examine
        examined = []

        def monotonic():
            return clock[0]

        def slow_first_examine(ctx, key):
            examined.append(key)
            result = real_examine(ctx, key)
            if len(examined) == 1:
                clock[0] += R.EXAMINE_BUDGET_SECONDS + 1
            return result

        with mock.patch.object(R.time, "monotonic", side_effect=monotonic), \
                mock.patch.object(R, "_examine", side_effect=slow_first_examine):
            result = self.sweep(0)
        self.assertEqual(result["examined"], 1)
        self.assertEqual(len(examined), 1)
        self.assertEqual(self.kept(result, "not-terminal"), 1)

    def test_remove_deadline_stops_before_starting_another_candidate(self):
        homes = [self.add(f"att-remove-deadline-{index}") for index in range(2)]
        clock = [0.0]
        real_remove = R._remove_leaf
        real_detach = R._detach
        detach_deadlines = []

        def monotonic():
            return clock[0]

        def record_detach(*args, **kwargs):
            detach_deadlines.append(args[6])
            return real_detach(*args, **kwargs)

        def advance_after_first_remove(*args, **kwargs):
            outcome = real_remove(*args, **kwargs)
            if outcome == "removed" and len(detach_deadlines) == 1:
                clock[0] = R.REMOVE_DEADLINE_SECONDS + 1
            return outcome

        with mock.patch.object(R.time, "monotonic", side_effect=monotonic), \
                mock.patch.object(R, "_detach", side_effect=record_detach), \
                mock.patch.object(R, "_remove_leaf", side_effect=advance_after_first_remove):
            result = self.sweep(8 / 7)
        self.assertEqual(len(detach_deadlines), 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.kept(result, "deferred"), 1)
        self.assertEqual(sum(not home.exists() for home in homes), 1)

    def test_leftover_removal_shares_the_cap(self):
        retiring = self.root / "homes/worker-retention/retiring"
        retiring.mkdir(parents=True, mode=0o700)
        for index in range(3):
            name = f"{self.key(f'att-l{index}')}.{index + 1}"
            leaf = retiring / name
            leaf.mkdir()
            (leaf / "file").write_text("x")
            st = os.stat(leaf)
            (retiring / (name + ".journal")).write_text(json.dumps(
                {"schema": 1, "key": self.key(f"att-l{index}"), "name": name,
                 "identity": [st.st_dev, st.st_ino, st.st_uid]}))
        homes = [self.add(f"att-n{index}") for index in range(3)]
        self.sweep(0)
        # Finished leaves come first and spend the same cap.
        retiring.mkdir(exist_ok=True)
        for index in range(3):
            name = f"{self.key(f'att-l{index}')}.{index + 11}"
            leaf = retiring / name
            leaf.mkdir()
            (leaf / "file").write_text("x")
            st = os.stat(leaf)
            (retiring / (name + ".journal")).write_text(json.dumps(
                {"schema": 1, "key": self.key(f"att-l{index}"), "name": name,
                 "identity": [st.st_dev, st.st_ino, st.st_uid]}))
        with mock.patch.object(R, "REMOVE_LIMIT", 4):
            result = self.sweep(8 / 7)
        self.assertEqual(result["detached_leftover"], 3)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(sum(1 for path in homes if not path.exists()), 1)
        self.assertEqual(sorted(path.name for path in retiring.iterdir()), [])

    def test_leftover_removal_shares_the_default_cap(self):
        homes = [self.add(f"att-default-cap-{index}") for index in range(3)]
        self.sweep(0)
        retiring = self.root / "homes/worker-retention/retiring"
        retiring.mkdir(parents=True, mode=0o700, exist_ok=True)
        for index in range(R.REMOVE_LIMIT - 1):
            key = self.key(f"att-default-leftover-{index}")
            name = f"{key}.{index + 1}"
            leaf = retiring / name
            leaf.mkdir()
            (leaf / "file").write_text("x")
            st = os.stat(leaf)
            (retiring / (name + ".journal")).write_text(json.dumps({
                "schema": 1, "key": key, "name": name,
                "identity": [st.st_dev, st.st_ino, st.st_uid]}))

        result = self.sweep(8 / 7)
        self.assertEqual(result["detached_leftover"], R.REMOVE_LIMIT - 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(sum(not path.exists() for path in homes), 1)
        self.assertEqual(sorted(path.name for path in retiring.iterdir()), [])

    def test_unfinishable_leftover_does_not_starve_new_removals(self):
        retiring = self.root / "homes/worker-retention/retiring"
        retiring.mkdir(parents=True, mode=0o700)
        stuck = retiring / f"{self.key('att-stuck')}.1"
        stuck.mkdir()
        (stuck / "dir").mkdir()
        (stuck / "dir" / "file").write_text("x")
        st = os.stat(stuck)
        (retiring / (stuck.name + ".journal")).write_text(json.dumps(
            {"schema": 1, "key": self.key("att-stuck"), "name": stuck.name,
             "identity": [st.st_dev, st.st_ino, st.st_uid]}))
        os.chmod(stuck / "dir", 0o500)
        self.addCleanup(lambda: os.chmod(stuck / "dir", 0o700) if (stuck / "dir").exists() else None)
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        homes = [self.add(f"att-s{index}") for index in range(4)]
        self.sweep(0)
        with mock.patch.object(R, "REMOVE_LIMIT", 4):
            result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 3)
        self.assertEqual(result["partial"], 1)
        self.assertEqual(sum(not path.exists() for path in homes), 3)

    def test_five_partial_leftovers_rotate_and_leave_a_slot_for_new_homes(self):
        homes = [self.add(f"att-fair-{n}") for n in range(3)]
        self.sweep(0)
        retiring = self.root / "homes/worker-retention/retiring"
        stuck = set()
        for n in range(5):
            name = f"{self.key(f'att-partial-{n}')}.{n + 1}"
            leaf = retiring / name
            leaf.mkdir()
            st = leaf.stat()
            (retiring / (name + ".journal")).write_text(json.dumps({
                "schema": 1, "key": self.key(f"att-partial-{n}"), "name": name,
                "identity": [st.st_dev, st.st_ino, st.st_uid]}))
            stuck.add(name)
        real_remove = R._remove_leaf
        touched = []
        def remove(layout, name, ident, deadline):
            touched.append(name)
            return "partial" if name in stuck else real_remove(layout, name, ident, deadline)
        with mock.patch.object(R, "_remove_leaf", side_effect=remove), \
                mock.patch.object(R, "REMOVE_LIMIT", 4):
            for window in (8 / 7, 9 / 7, 10 / 7):
                before = len(touched)
                result = self.sweep(window)
                self.assertLessEqual(len(touched) - before, 4)
                self.assertEqual(result["removed"], 1)
        self.assertTrue(stuck <= set(touched))
        self.assertFalse(any(home.exists() for home in homes))

    def test_torn_latest_registry_row_cannot_restore_old_terminal_authority(self):
        home = self.add("att-torn")
        self.sweep(0)
        with self.jobs.open("a") as out:
            out.write("2026-10-16T00:00:00Z\topen\t/repo\t/wt\tatt-torn\tattempt_id=att-torn")
        self.assertEqual(self.sweep((8) / 7), {"skipped": "registry-unreadable"})
        self.assertTrue(home.is_dir())
        self.assertEqual(self.state()["last_pass_ns"], self.now + int(8 / 7 * RETENTION_NS))
        self.assertEqual(R.sweep(self.jobs, now_ns=self.now + int(8 / 7 * RETENTION_NS) + 1),
                         {"skipped": "not-due"})

    def test_foreign_registry_lock_defers_promptly(self):
        home = self.add("att-busy")
        self.sweep(0)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,os,sys,time;"
             "f=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);"
             "fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(3)",
             str(self.jobs) + ".lock"], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            began = time.monotonic()
            result = self.sweep((8) / 7)
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 1.0)
            self.assertIsNone(holder.poll())
            self.assertEqual(result["kept"].get("registry-lock-busy"), 1)
            self.assertTrue(home.is_dir())
        finally:
            holder.terminate()
            holder.wait(timeout=3)
            holder.stdout.close()

    def test_brief_registry_lock_release_allows_same_sweep_removal(self):
        home = self.add("att-brief-lock")
        self.sweep(0)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,os,sys,time;"
             "f=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);"
             "fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(.05);"
             "fcntl.flock(f,fcntl.LOCK_UN);os.close(f)", str(self.jobs) + ".lock"],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            began = time.monotonic()
            result = self.sweep((8) / 7)
            elapsed = time.monotonic() - began
            holder.wait(timeout=2)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(result["removed"], 1, result)
            self.assertFalse(home.exists())
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=2)
            holder.stdout.close()

    def test_due_homes_progress_across_busy_and_released_registry_sweeps(self):
        homes = [self.add(f"att-cursor-{number}") for number in range(6)]
        self.sweep(0)
        fd = os.open(str(self.jobs) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            blocked = self.sweep((8) / 7)
            self.assertEqual(blocked["removed"], 0)
            self.assertGreater(blocked["kept"].get("registry-lock-busy", 0), 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        removed = 0
        for window in (8.1 / 7, 8.2 / 7, 8.3 / 7):
            removed += self.sweep(window)["removed"]
        self.assertEqual(removed, len(homes))
        self.assertFalse(any(home.exists() for home in homes))

    def test_busy_registry_and_continuous_unrelated_delivery_writer_still_remove_home(self):
        home = self.add("att-concurrent-cleanup")
        delivery = self.root / "pending-delivery" / ("f" * 64) / ("delivery-" + "7" * 32 + ".json")
        self.delivery_record(delivery, "att-unrelated-writer", "att-parent", state="acked")
        self.sweep(0)
        script = r'''import fcntl, json, os, sys, time
jobs, record = sys.argv[1:]
lock_path = record + ".lock"
raw = open(record, "rb").read()
needle = b'"attempts": 0'
offset = raw.find(needle)
if offset < 0:
    raise SystemExit("attempts field missing")
print("ready", flush=True)
for number in range(300):
    with open(jobs + ".lock", "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with open(jobs, "ab") as registry:
            registry.write(("2026-10-19T00:00:00Z\tdone\t/repo\t/wt\t"
                            f"att-noise-{number}\tattempt_id=att-noise-{number}\n").encode())
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    record_fd = os.open(record, os.O_RDWR)
    os.pwrite(record_fd, str(number % 2).encode(), offset + len(b'"attempts": '))
    os.close(record_fd)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    time.sleep(0.002)
print("writes=300", flush=True)
'''
        writer = subprocess.Popen(
            [sys.executable, "-c", script, str(self.jobs), str(delivery)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(writer.stdout.readline().strip(), "ready")
            time.sleep(0.03)
            result = self.sweep((8) / 7)
            stdout, stderr = writer.communicate(timeout=3)
            self.assertEqual(writer.returncode, 0, stderr)
            self.assertIn("writes=300", stdout)
            self.assertEqual(result["removed"], 1, result)
            self.assertFalse(home.exists())
        finally:
            if writer.poll() is None:
                writer.terminate()
                writer.wait(timeout=2)
            writer.stdout.close()
            writer.stderr.close()

    def test_large_delivery_index_is_prepared_outside_short_registry_hold(self):
        home = self.add("att-large-index")
        group = self.root / "pending-delivery" / ("c" * 64)
        for number in range(1024):
            self.delivery_record(group / f"delivery-{number:032x}.json",
                                 f"att-unrelated-{number}", state="acked")
        self.sweep(0)
        active = [False]
        held = []
        lock = R._registry_lock
        snapshot, index, declared = R._Snapshot, R._delivery_index, R._declared_index

        @contextlib.contextmanager
        def measured(jobs):
            with lock(jobs) as admitted:
                active[0] = True
                start = time.monotonic()
                try:
                    yield admitted
                finally:
                    held.append(time.monotonic() - start)
                    active[0] = False

        def outside(fn):
            def checked(*args, **kwargs):
                self.assertFalse(active[0], fn.__name__ + " ran under jobs.log.lock")
                return fn(*args, **kwargs)
            return checked

        with mock.patch.object(R, "_registry_lock", measured), \
                mock.patch.object(R, "_Snapshot", outside(snapshot)), \
                mock.patch.object(R, "_delivery_index", outside(index)), \
                mock.patch.object(R, "_declared_index", outside(declared)):
            result = self.sweep((8) / 7)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())
        self.assertEqual(len(held), 2)
        self.assertTrue(all(duration < 0.25 for duration in held), held)

    def test_complete_blank_and_malformed_registry_lines_do_not_poison_pass(self):
        home = self.add("att-noisy")
        with self.jobs.open("a") as out:
            out.write("\nmalformed\n")
        self.assertEqual(self.sweep(0)["eligible"], 1)
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_corrupt_state_preserves_valid_completion_clock(self):
        home = self.add("att-k1")
        self.sweep(0)
        (self.root / "homes/worker-retention/state.json").write_text("{corrupt")
        self.assertEqual(self.sweep((8) / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_corrupt_state_restarts_unparseable_completion_clock(self):
        home = self.add("att-k1-unparseable")
        self.set_timestamp("att-k1-unparseable", "invalid")
        self.sweep(0)
        (self.root / "homes/worker-retention/state.json").write_text("{corrupt")
        self.assertEqual(self.sweep((8) / 7)["removed"], 0)
        self.assertTrue(home.is_dir())
        self.assertEqual(self.state()["eligible_since"][home.name], self.now + int(8 / 7 * RETENTION_NS))
        self.assertEqual(self.sweep((16) / 7)["removed"], 1)

    def test_oversized_registry_and_unreadable_registry_skip_the_pass(self):
        home = self.add("att-k2")
        with mock.patch.object(R, "MAX_REGISTRY_BYTES", 1):
            self.assertEqual(self.sweep(0), {"skipped": "registry-oversized"})
        self.jobs.unlink()
        self.assertEqual(self.sweep((8) / 7), {"skipped": "registry-unreadable"})
        self.assertTrue(home.is_dir())

    def test_sweep_best_effort_swallows_everything(self):
        with mock.patch.object(R, "sweep", side_effect=RuntimeError("boom")):
            R.sweep_best_effort(self.jobs)


class Views(Fixture):
    def test_held_view_locks_do_not_block_fleet_or_registry_writers(self):
        home = self.add("att-locked")
        self.sweep(0)
        lock = self.root / "homes/worker-retention/view.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = time.monotonic()
            self.assertFalse(R.note_view(self.jobs, str(home)))
            self.assertLess(time.monotonic() - start, 0.5)
            fcntl.flock(fd, fcntl.LOCK_SH)
            start = time.monotonic()
            result = self.sweep((8) / 7)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(self.kept(result, "view-lock-busy"), 1)
            writer = os.open(str(self.jobs) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(writer)
        finally:
            os.close(fd)
        self.assertTrue(home.is_dir())

    def test_missing_home_after_view_lock_writes_no_marker(self):
        home = self.add("att-vanished")
        real = R._flock
        @contextlib.contextmanager
        def move_after_lock(*args, **kwargs):
            with real(*args, **kwargs) as held:
                home.rename(self.outside / "vanished")
                yield held
        with mock.patch.object(R, "_flock", move_after_lock):
            self.assertFalse(R.note_view(self.jobs, str(home)))
        self.assertFalse((self.root / "homes/worker-retention/views" / self.key("att-vanished")).exists())

    def test_marker_refreshes_at_its_window_boundary_and_only_for_a_present_home(self):
        home = self.add("att-w1")
        stamp = 1_800_000_000.0
        self.assertTrue(R.note_view(self.jobs, str(home), now=stamp))
        mark = self.root / "homes/worker-retention/views" / self.key("att-w1")
        self.assertTrue(mark.is_file())
        first_mtime = mark.stat().st_mtime_ns
        self.assertTrue(R.note_view(self.jobs, str(home), now=stamp + R.VIEW_REFRESH_SECONDS - 1))
        self.assertEqual(mark.stat().st_mtime_ns, first_mtime)
        self.assertTrue(R.note_view(self.jobs, str(home), now=stamp + R.VIEW_REFRESH_SECONDS))
        self.assertEqual(mark.stat().st_mtime_ns, int((stamp + R.VIEW_REFRESH_SECONDS) * 1e9))
        old = stamp - 2 * R.VIEW_REFRESH_SECONDS
        os.utime(mark, (old, old))
        self.assertTrue(R.note_view(self.jobs, str(home), now=stamp))
        self.assertEqual(mark.stat().st_mtime_ns, int(stamp * 1e9))
        gone = self.workers / ("e" * 32)
        self.assertFalse(R.note_view(self.jobs, str(gone), now=stamp))
        self.assertFalse((self.root / "homes/worker-retention/views" / gone.name).exists())
        self.assertFalse(gone.exists())

    def test_shared_and_outside_homes_are_ignored(self):
        shared = self.root / "homes" / "codex" / ("a" * 32)
        shared.mkdir(parents=True)
        self.assertTrue(R.note_view(self.jobs, str(shared)))
        for value in (str(shared), str(self.outside / ("a" * 32)), "relative/" + "a" * 32, "", None):
            if value != str(shared):
                self.assertFalse(R.note_view(self.jobs, value))
        self.assertFalse((self.root / "homes/worker-retention/views").exists())

    def test_nonhex_directory_under_workers_is_excluded_without_marker(self):
        other = self.workers / "not-a-candidate"
        other.mkdir()
        self.assertTrue(R.note_view(self.jobs, str(other)))
        self.assertFalse(R.note_view(self.jobs, str(self.workers / "missing-name")))
        self.assertFalse((self.root / "homes/worker-retention/views").exists())

    def test_a_home_that_is_a_link_is_not_marked(self):
        (self.outside / "x").mkdir()
        link = self.workers / ("a" * 32)
        link.symlink_to(self.outside / "x")
        self.assertFalse(R.note_view(self.jobs, str(link)))


if __name__ == "__main__":
    unittest.main()
