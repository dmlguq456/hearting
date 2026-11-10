"""Automatic retention of expired per-attempt worker homes (issue #564).

Every fixture lives in a temporary dispatch state root; nothing reads or
touches a live registry or home.
"""
import contextlib
import fcntl
import json
import os
import select
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
from split_module_fixture import patch_split  # noqa: E402

RETENTION_NS = R.IDLE_SECONDS * 10**9
SOURCE = str(ROOT)


class VirtualDeliveries:
    """Virtual entry/body seam; only a few real, command-owned directories.

    Use production validation, clocks, state, admissions and removal unchanged.
    The opaque position seam is the same one used by the real cookie backend.
    """
    def __init__(self, root, now, *, groups=1, count=4097, open_refs=(), terminal_ref=None,
                 malformed=False, tombstones=False, terminal_state="acked", recipient_keys=None):
        self.root, self.now = Path(root), now
        self.groups, self.count = groups, count
        self.open_refs, self.terminal_ref = list(open_refs), terminal_ref
        self.malformed, self.tombstones = malformed, tombstones
        self.terminal_state = terminal_state
        self.reads = self.entries = self.stats = 0
        self.paths = []
        (self.root / "pending-delivery").mkdir(mode=0o700, exist_ok=True)
        for i in range(groups):
            group_name = P.recipient_digest(recipient_keys[i]) if recipient_keys else f"{i + 1:064x}"
            group = self.root / "pending-delivery" / group_name
            group.mkdir(mode=0o700, exist_ok=True)
            self.paths.append(group)
        self.real_page, self.real_read, self.real_stamp = R._directory_page, R._read_json, R._stamp
        self.stack = contextlib.ExitStack()

    def _range(self, group):
        i = self.paths.index(group)
        low, high = self.count * i // self.groups, self.count * (i + 1) // self.groups
        return low, high + (len(self.open_refs) if i == self.groups - 1 else 0)

    def _virtual(self, path):
        path = Path(path)
        if path.parent in self.paths and path.name.startswith("delivery-") and path.name.endswith(".json"):
            try:
                index = int(path.name[9:-5], 16)
            except ValueError:
                return None
            low, high = self._range(path.parent)
            if low <= index < high:
                return path
        return None

    def page(self, path, cookie, limit, deadline):
        if Path(path) == self.root / "pending-delivery":
            extras = sorted(p.name for p in Path(path).iterdir() if p not in self.paths)
            names = [".", "..", *[p.name for p in self.paths], *extras]
            end = min(len(names), cookie + limit)
            return ([(names[i], i + 1, 4) for i in range(cookie, end)],
                    cookie == len(names), self.real_stamp(path))
        if Path(path) not in self.paths:
            return self.real_page(path, cookie, limit, deadline)
        low, high = self._range(Path(path))
        # Real normal-API publications remain visible when a group is revisited.
        extras = sorted(p.name for p in Path(path).iterdir())
        length = high - low
        end = min(length + len(extras), cookie + min(limit, 317))
        suffix = ".json.pruned" if self.tombstones else ".json"
        entries = [((f"delivery-{i + low:032x}" + suffix if i < length else extras[i - length]), i + 1, 8)
                   for i in range(cookie, end)]
        self.entries += len(entries)
        return entries, cookie == length + len(extras), self.real_stamp(path)

    def read(self, path, *args, **kwargs):
        virtual = self._virtual(path)
        if virtual is None:
            return self.real_read(path, *args, **kwargs)
        self.reads += 1
        index = int(virtual.name[9:-5], 16)
        value = dict.fromkeys(P.REQUIRED_FIELDS)
        ids, parent, state = [f"att-history-{index}"], "-", self.terminal_state
        if index >= self.count:
            ids, parent = self.open_refs[index - self.count]
            state = "pending"
        if index == 0 and self.terminal_ref:
            ids = [self.terminal_ref]
        value.update(schema_version=1, recipient_kind="codex-native-queue", attempts=0,
                     delivery_id=virtual.name[:-5], recipient_digest=virtual.parent.name,
                     attempt_ids=ids, parent_attempt_id=parent, state=state,
                     claim_authority=next(iter(P.CLAIM_AUTHORITIES)))
        if self.malformed and index == self.count - 1:
            value["attempt_ids"] = ["malformed"]
        return value, self.now

    def stamp(self, path):
        virtual = self._virtual(path)
        if virtual is None:
            return self.real_stamp(path)
        self.stats += 1
        info = list(self.real_stamp(virtual.parent))
        info[1] = int(virtual.name[9:-5], 16) + 100000
        info[2] = __import__("stat").S_IFREG | 0o600
        info[4:] = [100, self.now, self.now]
        return tuple(info)

    def __enter__(self):
        self.stack.enter_context(mock.patch.object(R, "_directory_page", self.page))
        self.stack.enter_context(mock.patch.object(R, "_read_json", self.read))
        self.stack.enter_context(mock.patch.object(R, "_stamp", self.stamp))
        return self

    def __exit__(self, *exc):
        self.stack.close()


@contextlib.contextmanager
def normal_delivery_writer(root, recipient, attempt, *, pinned=False, relation="attempt", gated=False,
                           hold=False):
    """A real separate-process writer, optionally the pre-G03 single Git blob.

    Only observe entry into reference admission; never replace its lock/API.
    Settle this owned child before the fixture can be retired, even on failure.
    """
    code = r'''
import json,subprocess,sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/utilities')
import dispatch_attempt_reference as ar
if sys.argv[5]=='pinned':
    source=subprocess.check_output(['git','show','f0f6788418d2c50fb506e1bd0be43162c09c43d7:utilities/dispatch_pending_delivery.py'],cwd=sys.argv[1])
    p=types.ModuleType('g03_pinned_writer');exec(compile(source,'git-blob/dispatch_pending_delivery.py','exec'),p.__dict__)
else:
    import dispatch_pending_delivery as p
real_open=ar._open_locked
announced=False
def observe(*args,**kwargs):
    global announced
    if not announced:
        print('reference-attempt',flush=True);announced=True
    return real_open(*args,**kwargs)
ar._open_locked=observe
if sys.argv[7]=='gated':
    sys.stdin.readline()
if sys.argv[7]=='hold':
    # Pause inside the record's reference admission, after the writer's own
    # lock-time chmod and before the payload is published (no lock/API change).
    real_write=p._write_unlocked
    def held_write(*args,**kwargs):
        print('record-admitted',flush=True)
        sys.stdin.readline()
        return real_write(*args,**kwargs)
    p._write_unlocked=held_write
attempt=sys.argv[4];receipt={'schema_version':2,'fixture':'g03-normal-writer'}
v=p.create(Path(sys.argv[2]),recipient_kind='codex-native-queue',recipient_key=sys.argv[3],
    delivery_id='delivery-'+'f'*32,session_generation='',session_generation_supported='0',
    attempt_ids=[attempt] if sys.argv[6]=='attempt' else ['att-normal-child'],
    parent_attempt_id='-' if sys.argv[6]=='attempt' else attempt,
    route_id='rt-g03',route_node='execute',receipt=receipt,
    receipt_digest=p._canonical_receipt_digest(receipt),row_revisions={})
print(json.dumps({'state':v['state']}),flush=True)
'''
    child = subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT), str(root),
                              recipient, attempt, "pinned" if pinned else "current", relation,
                              "hold" if hold else "gated" if gated else "immediate"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        yield child
    finally:
        if (gated or hold) and child.poll() is None:
            try:
                child.stdin.write("go\n"); child.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
            raise AssertionError(f"normal writer timed out: {stdout} {stderr}")
        if child.returncode:
            raise AssertionError(f"normal writer exit {child.returncode}: {stdout} {stderr}")


def child_line(child, deadline):
    """One stdout line from a fixture child; "" at EOF, None at the deadline.

    Read the pipe a byte at a time. select() only sees the OS pipe, so a
    buffered readline() can pull a second line that already arrived into the
    file object's buffer, and the next select() then waits out the deadline
    although the line is there (a busy runner reads both writer lines at once).
    """
    fd = child.stdout.fileno()
    data = b""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        if not select.select([fd], [], [], min(1.0, left))[0]:
            continue
        byte = os.read(fd, 1)
        if not byte or byte == b"\n":
            return data.decode("utf-8", "replace")
        data += byte


def wait_for_reference_attempt(child):
    line = child_line(child, time.monotonic() + 10)
    if line is None:
        raise AssertionError("normal writer did not reach reference admission")
    if line.strip() != "reference-attempt":
        raise AssertionError("normal writer failed before reference admission")


def wait_for_record_admitted(child):
    deadline = time.monotonic() + 10
    while True:
        line = child_line(child, deadline)
        if line is None or not line.strip():
            break
        if line.strip() == "record-admitted":
            return
    raise AssertionError("normal writer did not hold inside its record admission")


def r1_publication_run(case, *, attempt, paths, sweep, at, history_time, timing, pinned, relation,
                       new_group, real_history=0, first_at=None, group_index=0):
    """One real old/current writer against a two-pass (or large single-pass) observation.

    No stamp value is forged. The writer is a separate process; it publishes through
    its own record admission. Timings (see the R1 plan): T1 the writer is inside its
    record admission before the first root snapshot and publishes after pass 1; T2 it
    enters between the two root snapshots of the resumed pass and publishes before the
    candidate check; T3 it publishes between the passes; T4 it publishes just before
    the final candidate check (outside the final admission). `L1` is T2 over a large
    single pass of real files (`real_history` per group).
    """
    recipients = ["r1-a", "r1-b", "r1-c"]
    recipient = "r1-new" if new_group else recipients[group_index]
    base = case.root / "pending-delivery"
    events = {}
    stack = contextlib.ExitStack()
    with stack:
        if real_history:
            for recipient_key in recipients:
                group = P.record_directory(case.root, recipient_key)
                group.mkdir(parents=True, mode=0o700, exist_ok=True)
                for i in range(real_history):
                    Fixture.delivery_record(case, group / f"delivery-{i:032x}.json", f"att-history-{i}",
                                            parent=f"att-history-parent-{i}")
        else:
            stack.enter_context(VirtualDeliveries(case.root, history_time, groups=3,
                                                   recipient_keys=recipients))
        child = [None]
        writer = [None]

        def start_writer(hold):
            writer[0] = normal_delivery_writer(case.root, recipient, attempt, pinned=pinned,
                                               relation=relation, hold=hold)
            child[0] = stack.enter_context(writer[0])
            events["hold"] = hold
            if hold:
                wait_for_record_admitted(child[0])

        def finish_writer():
            running = child[0]
            if running is None or running.poll() is not None:
                return
            if events.get("hold"):
                try:
                    running.stdin.write("go\n"); running.stdin.flush()
                except BrokenPipeError:
                    pass
            running.wait(timeout=10)

        real_stamp, real_late = R._stamp, R._late_candidate_delivery_change
        armed = {"stamp": False, "late": False}

        def stamp(path):
            if armed["stamp"] and child[0] is None and Path(path) == base:
                sampled = real_stamp(path)
                start_writer(hold=True)
                return sampled
            return real_stamp(path)

        def late(ctx, selected, deadline, binding=None):
            if armed["late"] and not events.get("published"):
                events["published"] = True
                if timing == "T4":
                    start_writer(hold=False)
                    finish_writer()
                else:
                    finish_writer()
            return real_late(ctx, selected, deadline, binding)

        before = None
        with mock.patch.object(R, "_stamp", stamp), mock.patch.object(R, "_late_candidate_delivery_change", late):
            if real_history:
                # A large single pass: establish the idle clock first, then observe once.
                if first_at is not None:
                    events["first"] = sweep(case.jobs, now_ns=first_at)
                with AR.admission(case.root, [attempt]):
                    before = P.reference_token(case.root, attempt, initialize=True)
                armed["stamp"] = armed["late"] = True
                events["second"] = sweep(case.jobs, now_ns=at)
            else:
                armed["stamp"] = timing in ("T1",)
                events["first"] = sweep(case.jobs, now_ns=at)
                assert events["first"]["removed"] == 0, events["first"]
                before = P.reference_token(case.root, attempt)
                if timing == "T1":
                    armed["stamp"] = False
                    finish_writer()
                    events["after_first_token_unchanged"] = before == P.reference_token(case.root, attempt)
                elif timing == "T3":
                    start_writer(hold=False)
                    finish_writer()
                elif timing == "T2":
                    armed["stamp"] = True
                armed["late"] = timing in ("T2", "T4")
                events["second"] = sweep(case.jobs, now_ns=at + 301 * 10**9)
        finish_writer()
        record = P.read(case.root, recipient, "delivery-" + "f" * 32)
        assert record is not None, ("the writer never published", events)
        return {"timing": timing, "pinned": pinned, "relation": relation, "new_group": new_group,
                "second": events["second"], "exists": [p.exists() for p in paths],
                "record_state": record["state"],
                "token_unchanged": before == P.reference_token(case.root, attempt)}


class VirtualRecipientGroups:
    """Many recipient directories with one historical tombstone each, in memory."""
    def __init__(self, root, count):
        self.base, self.count = Path(root) / "pending-delivery", count
        self.base.mkdir(exist_ok=True)
        self.real_page, self.real_stamp = R._directory_page, R._stamp
        self.entries = 0
        self.stack = contextlib.ExitStack()

    def _group(self, path):
        path = Path(path)
        return (path.parent == self.base and R.SHA256_RE.fullmatch(path.name)
                and int(path.name, 16) < self.count)

    def stamp(self, path):
        if self._group(path):
            value = list(self.real_stamp(self.base))
            value[1] += int(Path(path).name, 16) + 1
            return tuple(value)
        return self.real_stamp(path)

    def page(self, path, cookie, limit, deadline):
        if Path(path) == self.base:
            end = min(self.count, cookie + min(limit, 317))
            entries = [(f"{i:064x}", i + 1, 4) for i in range(cookie, end)]
            eof = cookie == self.count
        elif self._group(path):
            entries = [("delivery-" + "0" * 32 + ".json.pruned", 1, 8)] if cookie == 0 else []
            eof = cookie != 0
        else:
            return self.real_page(path, cookie, limit, deadline)
        self.entries += len(entries)
        return entries, eof, self.stamp(path)

    def __enter__(self):
        self.stack.enter_context(mock.patch.object(R, "_directory_page", self.page))
        self.stack.enter_context(mock.patch.object(R, "_stamp", self.stamp))
        return self

    def __exit__(self, *exc):
        self.stack.close()


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

    def start_provider(self, lockdir):
        """A process shaped like the title provider: it names its lockdir in argv."""
        provider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                     "--lockdir", str(lockdir)])
        self.addCleanup(lambda: (provider.kill(), provider.wait(timeout=10)))
        return provider

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


class HomeReferenceRelevanceReuseTest(Fixture):
    """HL-11: a Codex prompt that attached N completion notices asked this 4N
    times (claim, submission fence, send, ACK), and an attempt outside the
    registry tail re-read every worker HOME manifest each time (2,277 live).
    Counted reads; a fake settle margin instead of waiting."""

    def setUp(self):
        super().setUp()
        for name in ("_REFERENCE_TAILS", "_REFERENCE_HOMES"):
            cache = getattr(R, name, None)
            if cache is not None:
                self.enterContext(mock.patch.dict(cache, clear=True))
        # Every listing here counts as older than one timestamp tick.
        self.enterContext(mock.patch.object(R, "_REFERENCE_SETTLED_NS", -(10 ** 18), create=True))

    def _home(self, attempt, worker_type="stage"):
        home = self.home_path(attempt, worker_type=worker_type)
        home.mkdir(mode=0o700)
        (home / "worker-home.json").write_text(json.dumps(
            {"schema": 1, "source": SOURCE, "harness": "claude", "worker_type": worker_type}))
        return home

    def _ask(self, *attempt_sets):
        reads = []
        real = R._read_json

        def counted(path, *args, **kwargs):
            if path == "worker-home.json":
                reads.append(path)
            return real(path, *args, **kwargs)

        with mock.patch.object(R, "_read_json", side_effect=counted):
            answers = [R.home_reference_relevant(self.root, attempts) for attempts in attempt_sets]
        return answers, len(reads)

    def test_one_home_listing_serves_every_question_while_the_directory_is_unchanged(self):
        for index in range(20):
            self._home(f"att-{index:032x}")
        asked = [[f"att-{0xfeed00 + notice:032x}"] for notice in range(4) for _lock in range(4)]
        answers, reads = self._ask(*asked)
        self.assertEqual(answers, [False] * 16)
        # Base: 16 questions x 20 manifests.
        self.assertEqual(reads, 20)

    def test_a_new_home_or_a_new_registry_row_is_seen_by_the_next_question(self):
        known = "att-" + "a" * 32
        later = "att-" + "b" * 32
        self._home(known)
        self.assertEqual(self._ask([known], [later]), ([True, False], 1))
        self._home(later)  # the directory changed: listed again
        self.assertEqual(self._ask([later]), ([True], 2))
        self.row(later)  # registered in the tail: no HOME question at all
        self.assertEqual(self._ask([later], [later]), ([False, False], 0))

    def test_a_listing_taken_right_after_a_change_is_not_kept(self):
        self._home("att-" + "c" * 32)
        with mock.patch.object(R, "_REFERENCE_SETTLED_NS", 10 ** 15, create=True):
            self.assertEqual(self._ask(["att-" + "d" * 32], ["att-" + "d" * 32])[1], 2)


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
            resumed = R.sweep(self.jobs, now_ns=self.now + int(2.02 * RETENTION_NS))
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
    def legacy_notice(self, session_id, key):
        # Persist the old producer's exact notice shape. New ordinary notices
        # have no synthetic attempt; retained legacy records remain readable.
        import hashlib
        receipt = NOTICE.validate({"schema_version": 1, "kind": "notice", "subject": "Status",
                                   "text": "Read this", "required_action": "read-notice", "key": key})
        digest = hashlib.sha256(f"notice\0{key}".encode()).hexdigest()
        tag = "notice-" + digest[:32]
        return P.create(self.root, recipient_kind="claude-parent-runtime", recipient_key=session_id,
                        delivery_id="delivery-" + digest, session_generation="",
                        session_generation_supported="0", attempt_ids=[tag], parent_attempt_id=tag,
                        route_id="route-free", route_node="notice", receipt=receipt,
                        receipt_digest=NOTICE.receipt_digest(receipt), row_revisions={tag: "notice"})

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
        notice = self.legacy_notice("sess-notice", "retention-fixture")
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
        notice = self.legacy_notice("sess-mixed", "mixed-fixture")
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

    def test_partial_delivery_read_defers_without_claiming_unknown(self):
        home = self.add("att-d7")
        with mock.patch.object(R, "DELIVERY_READ_BUDGET", 1):
            folder = self.root / "pending-delivery" / ("c" * 64)
            folder.mkdir(parents=True)
            for index in range(3):
                self.delivery_record(folder / f"delivery-{index:032x}.json", f"att-other-{index}")
            result = self.expire()
        self.assertEqual(self.unknown(result, "delivery-index-unknown"), 0)
        self.assertEqual(result["deferred"], 1)
        self.assertTrue(home.is_dir())

    def producer_notice(self, harness, session_id, key):
        record = NOTICE.notify(harness, session_id, key=key, subject="Status",
                               text="Read this", jobs=self.jobs)
        self.assertIsNotNone(record)
        self.assertEqual(record["parent_attempt_id"], "")
        self.assertEqual(record["attempt_ids"], [])
        return record

    def test_ordinary_session_notice_from_producer_does_not_block_retention(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                home = self.add("att-ordinary-" + harness, harness=harness)
                self.producer_notice(harness, "sess-ordinary-" + harness, "ordinary-" + harness)
                self.assertEqual(self.sweep(0)["eligible"], 1)
                result = self.sweep(8 / 7)
                self.assertEqual(result["removed"], 1)
                self.assertFalse(home.exists())

    def test_ordinary_notice_with_corrupt_record_still_keeps_home(self):
        home = self.add("att-ordinary-corrupt")
        self.producer_notice("claude", "sess-corrupt", "ordinary-corrupt")
        folder = self.root / "pending-delivery" / ("d" * 64)
        folder.mkdir(parents=True)
        corrupt = folder / ("delivery-" + "d" * 32 + ".json")
        corrupt.write_text("{")
        stamp = self.now / 1e9
        os.utime(corrupt, (stamp, stamp))
        self.sweep(0)
        result = self.sweep(8 / 7)
        self.assertEqual(self.unknown(result, "delivery-index-unknown"), 1)
        self.assertEqual(result["unknown_causes"]["delivery-index-unknown"],
                         {"record-unreadable": 1})
        self.assertTrue(home.is_dir())

    def test_delivery_relation_and_observation_share_record_identities(self):
        ordinary = self.producer_notice("claude", "sess-share", "share-ordinary")
        legacy = self.legacy_notice("sess-share", "share-legacy")
        opened = self.publish_delivery("delivery-" + "5" * 32, "sess-share", ["att-share"], "-")
        paths = {name: self.root / "pending-delivery" / record["recipient_digest"]
                 / (record["delivery_id"] + ".json")
                 for name, record in (("ordinary", ordinary), ("legacy", legacy), ("open", opened))}
        # The observation and the late-change relation read through one function,
        # which is the record's identity decision plus its own path.
        for name, expected in (("ordinary", frozenset()), ("legacy", frozenset()),
                               ("open", frozenset({"att-share"}))):
            value = json.loads(paths[name].read_text())
            self.assertEqual(P.record_references(value, group=paths[name].parent.name,
                                                 delivery_id=paths[name].stem), expected, name)
            self.assertEqual(frozenset(P.record_attempt_identities(value)), expected, name)
        moved = json.loads(paths["open"].read_text())
        with self.assertRaises(P.PendingDeliveryError) as raised:
            P.record_references(moved, group="0" * 64, delivery_id=paths["open"].stem)
        self.assertEqual(raised.exception.detail, "record-path-mismatch")
        for name, expected in (("ordinary", "unrelated"), ("legacy", "unrelated"),
                               ("open", "related")):
            self.assertEqual(R._delivery_record_relation(paths[name], {"att-share"}), expected, name)
        for name in ("ordinary", "legacy"):
            self.assertEqual(R._delivery_record_relation(paths[name], {"att-other"}), "unrelated")

    def test_tombstones_do_not_spend_the_read_budget(self):
        home = self.add("att-tombstones")
        group = self.root / "pending-delivery" / ("a" * 64)
        group.mkdir(parents=True)
        count = R.DELIVERY_READ_BUDGET + 1
        for index in range(count):
            (group / f"delivery-{index:032x}.json.pruned").touch()
        # More tombstones than one pass may read records: they are never read,
        # so the first pass still finishes its observation.
        first = self.sweep(0)
        self.assertEqual((first["eligible"], first["delivery_reads"]), (1, 0), first)
        result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(home.exists())


class DeliveryPruneCarrier(Fixture):
    """The bounded SD-111 (7) delivery-record prune rides the HOME retention
    pass (reaper tail / selector-less reconcile) and runs before the pass
    builds its delivery index, so one run's HOME and log judgments see it."""

    DAY = 86400

    def group(self, number):
        import hashlib
        return hashlib.sha256(f"prune-group-{number}".encode()).hexdigest()

    def put(self, group, serial, state, age_days, attempt=None, **changes):
        path = self.root / "pending-delivery" / self.group(group) / f"delivery-{serial:032x}.json"
        self.delivery_record(path, attempt or f"att-{serial:032x}", state=state, **changes)
        stamp = time.time() - age_days * self.DAY
        os.utime(path, (stamp, stamp))
        return path

    def json_files(self):
        return sorted((self.root / "pending-delivery").glob("*/*.json"))

    def tombstones(self):
        return sorted((self.root / "pending-delivery").glob("*/*.json.pruned"))

    def last_prune(self):
        return self.state()["last_result"]["delivery_prune"]

    # --- (a) reduced shape --------------------------------------------------
    def test_reduced_backlog_drains_below_the_cap_and_unknown_clears_in_the_same_pass(self):
        home = self.add("att-cand")
        serial = 0
        for index in range(29):
            serial += 1
            self.put(index % 3, serial, "acked", 8 + index)
        for index in range(25):
            serial += 1
            self.put(index % 3, serial, "acked", 1 + index / 10)
        for index in range(7):
            serial += 1
            self.put(index % 3, serial, ("pending", "claimed", "sent-ambiguous")[index % 3], 1)
        self.assertEqual(len(self.json_files()), 61)
        with mock.patch.object(R, "DELIVERY_READ_BUDGET", 40), \
                mock.patch.object(P, "PRUNE_RECORD_LIMIT", 8):
            runs = [self.sweep(0) for _ in range(5)]
            remaining, completed = [], []
            for number, result in enumerate(runs, 1):
                remaining.append(result["delivery_prune"]["records_remaining"])
                # G03: more records than one pass reads is a resumable deferral of
                # the one candidate, never a global unknown.
                self.assertEqual(self.unknown(result, "delivery-index-unknown"), 0, (number, result))
                self.assertEqual(result["unknown"], {}, (number, result))
                if result["deferred"]:
                    self.assertEqual(self.kept(result, "delivery-read-deferred"), 1, (number, result))
                    self.assertLessEqual(result["delivery_reads"], 40, (number, result))
                    self.assertEqual(self.kept(result, "within-window"), 0, (number, result))
                else:
                    # The prune ran first, so the finished observation read only
                    # what was left, and the ordinary rule decides in the same pass.
                    completed.append(number)
                    self.assertEqual(self.kept(result, "within-window"), 1, (number, result))
                    self.assertLessEqual(result["delivery_reads"],
                                         result["delivery_prune"]["records_remaining"], (number, result))
            self.assertTrue(completed and completed[0] <= 3, (completed, runs))
            print("reduced-backlog observation-complete passes", completed)
            self.assertEqual(remaining, [53, 45, 37, 32, 32])
            self.assertEqual(runs[3]["delivery_prune"]["aged_seen"], 5)
            self.assertEqual(runs[4]["delivery_prune"]["pruned_records"], 0)
        self.assertEqual(len(self.json_files()), 32)
        self.assertEqual(len(self.tombstones()), 29)
        self.assertEqual(self.last_prune()["records_remaining"], len(self.json_files()))
        self.assertTrue(home.is_dir())

    # --- (b) live-shaped backlog (6,133 records, 4,096 reads per pass) ---------
    def test_live_shaped_backlog_drains_below_cap_within_bounded_passes(self):
        import math
        import builtins
        import dispatch_log_retention as LOGS
        AGED, RECENT, OPEN, OPEN_OLD, GROUPS = 2933, 2503, 698, 60, 64
        home = self.add("att-cand")
        logs = self.root / "logs"
        logs.mkdir(mode=0o700)
        log_file = logs / "att-log-cand.jsonl"
        log_file.write_text("log\n")
        ended = datetime.fromtimestamp(self.now / 10**9 - 15 * self.DAY, timezone.utc)
        self.timestamps["att-log-cand"] = ended.isoformat().replace("+00:00", "Z")
        self.row("att-log-cand", status="done", log_file=str(log_file))

        serial = 0
        aged, recent, openers, tombs = [], [], [], []
        for index in range(AGED):
            serial += 1
            state = ("acked", "rejected", "expired")[index % 3]
            aged.append(self.put(serial % GROUPS, serial, state, 8 + 26 * index / AGED))
        for index in range(RECENT):
            serial += 1
            recent.append(self.put(serial % GROUPS, serial, ("acked", "rejected")[index % 2],
                                   5.5 * index / RECENT))
        for index in range(OPEN):
            serial += 1
            old = 15 + index if index < OPEN_OLD else 5.5 * index / OPEN
            openers.append(self.put(serial % GROUPS, serial,
                                    ("pending", "claimed", "sent-ambiguous")[index % 3], old))
        for index in range(5):
            serial += 1
            victim = self.put(serial % GROUPS, serial, "acked", 20)
            value = json.loads(victim.read_text())
            # Production writes a tombstone inside the removal's reference admission.
            with AR.admission(self.root, P.record_references(value)):
                P._write_prune_tombstone(victim, value)
            victim.unlink()
            tombs.append(P.tombstone_path(victim))
        total = AGED + RECENT + OPEN
        self.assertEqual(len(self.json_files()), total)
        keep = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in recent + openers + tombs}
        aged_mtimes = {path: path.stat().st_mtime for path in aged}

        opened = []
        real_os_open, real_open, real_read_text = os.open, builtins.open, Path.read_text
        window = {"on": False}

        def watch(name):
            if window["on"] and ("jobs.log" in str(name) or "worker-home.json" in str(name)):
                opened.append(str(name))

        def os_open(path, *args, **kwargs):
            watch(path)
            return real_os_open(path, *args, **kwargs)

        def bi_open(file, *args, **kwargs):
            watch(file)
            return real_open(file, *args, **kwargs)

        def read_text(self_, *args, **kwargs):
            watch(self_)
            return real_read_text(self_, *args, **kwargs)

        real_prune = P.prune_bounded

        def measured_prune(*args, **kwargs):
            before = len(opened)
            window["on"] = True
            try:
                return real_prune(*args, **kwargs)
            finally:
                window["on"] = False
                measured_prune.opens.append(len(opened) - before)
        measured_prune.opens = []

        table, oldest_removed_ok = [], True
        first_below = None
        pruned_total = 0
        step = (R.DUE_SECONDS + 1) * 10**9
        with mock.patch.object(P, "prune_bounded", measured_prune), \
                self.replaced("open", os_open), mock.patch("builtins.open", bi_open), \
                mock.patch.object(Path, "read_text", read_text):
            for run in range(1, 13):
                at = self.now + run * step
                before = len(self.json_files())
                home_result = R.sweep(self.jobs, now_ns=at)
                log_result = LOGS.sweep(self.jobs, now_ns=at)
                self.assertNotIn("skipped", home_result, home_result)
                self.assertNotIn("skipped", log_result, log_result)
                prune = home_result["delivery_prune"]
                after = len(self.json_files())
                pruned_total += prune["pruned_records"]
                row = {
                    "run": run, "before": before, "after": after,
                    "pruned": prune["pruned_records"], "kept_open_aged": prune["kept_open_aged"],
                    "deferred": prune["deferred"], "budget_exhausted": prune["budget_exhausted"],
                    "home_unknown": home_result["unknown"], "home_kept": home_result["kept"],
                    "log_unknown": log_result["kept_reasons"].get("delivery-index-unknown", 0),
                    "log_removed": log_result["removed"],
                    "prune_body_reads": prune["body_reads"],
                    "prune_manifest_or_jobs_opens": measured_prune.opens[-1],
                    "prune_elapsed_ms": prune["elapsed_ms"],
                }
                table.append(row)
                self.assertLessEqual(prune["pruned_records"], P.PRUNE_RECORD_LIMIT, row)
                self.assertEqual(prune["body_reads"], prune["aged_seen"] - prune["deferred"], row)
                self.assertEqual(measured_prune.opens[-1], 0, row)
                self.assertEqual(prune["records_remaining"], after, row)
                # oldest first: nothing removed is younger than anything still waiting
                gone = [aged_mtimes[path] for path in aged if not path.exists()]
                waiting = [aged_mtimes[path] for path in aged if path.exists()]
                if gone and waiting:
                    self.assertLessEqual(max(gone), min(waiting), row)
                if first_below is None and after <= R.DELIVERY_READ_BUDGET:
                    first_below = run
                # G03: more records than one pass reads defers the candidate and resumes; it
                # is never a global unknown, before or after the first pass below the budget.
                self.assertEqual(self.unknown(home_result, "delivery-index-unknown"), 0, row)
                self.assertEqual(home_result["unknown"], {}, row)
                self.assertEqual(row["log_unknown"], 0, row)
                if run == 1:
                    # 5,600+ records cannot be read in one 4,096-read pass; a resumed
                    # observation may finish on a later pass while still over the budget.
                    self.assertGreaterEqual(self.kept(home_result, "delivery-read-deferred"), 1, row)
                    self.assertGreaterEqual(log_result["kept_reasons"].get("delivery-read-deferred", 0), 1, row)
                if pruned_total == AGED:
                    break
        print("\nDELIVERY-PRUNE-TABLE " + json.dumps(table, sort_keys=True))
        for row in table:
            print("run {run}: {before}->{after} pruned={pruned} open_aged={kept_open_aged} "
                  "deferred={deferred} unknown={home_unknown} kept={home_kept} "
                  "log_unknown={log_unknown} log_removed={log_removed} body_reads={prune_body_reads} "
                  "manifest/jobs opens={prune_manifest_or_jobs_opens} elapsed_ms={prune_elapsed_ms}".format(**row))
        smallest = min(row["pruned"] for row in table[:-1]) if len(table) > 1 else table[0]["pruned"]
        excess = total - R.DELIVERY_READ_BUDGET
        self.assertLessEqual(first_below, math.ceil(excess / smallest), table)
        if all(row["pruned"] == P.PRUNE_RECORD_LIMIT for row in table[:-1]):
            self.assertEqual(first_below, 4, table)
            self.assertEqual(len(table), 6, table)
        self.assertEqual(pruned_total, AGED)
        self.assertTrue(all(not path.exists() for path in aged))
        self.assertEqual(len(self.tombstones()), AGED + 5)
        self.assertEqual(len(self.json_files()), RECENT + OPEN)
        for path, expected in keep.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected, path)
        self.assertGreaterEqual(table[first_below - 1]["log_removed"], 1, table)
        self.assertFalse(log_file.exists())
        self.assertTrue(home.is_dir())
        self.assertTrue(any(k not in ("delivery-index-unknown", "delivery-read-deferred")
                            for k in table[-1]["home_kept"]),
                        "the HOME now fails on an ordinary rule, not on the index")

    # --- R1 (d) prune, unrelated publication and the final look together ------
    def test_prune_tombstones_and_unrelated_publication_every_pass_do_not_stretch_the_observation(self):
        import math
        home = self.add("att-cand")
        serial = 0
        for index in range(600):
            serial += 1
            self.put(serial % 16, serial, ("acked", "rejected", "expired")[index % 3], 8 + 20 * index / 600)
        for index in range(200):
            serial += 1
            self.put(serial % 16, serial, "acked", 2 * index / 200)
        extra = [700000]
        passes = []
        step = (R.DUE_SECONDS + 1) * 10**9
        at = self.now + 2 * RETENTION_NS
        for run in range(4):
            before_json = len(self.json_files())
            result = R.sweep(self.jobs, now_ns=at + run * step)
            passes.append(result)
            self.assertEqual(result["unknown"], {}, result)
            # tombstones and unlinks never cost a body read: only `.json` records are read
            self.assertLessEqual(result["delivery_reads"], before_json, result)
            if not home.exists():
                break
            for _ in range(5):
                extra[0] += 1
                self.publish_delivery(f"delivery-{extra[0]:032x}", "r1-unrelated-prune-group",
                                      [f"att-unrelated-{extra[0]}"], "-")
        self.assertFalse(home.exists(), passes)
        self.assertLessEqual(len(passes), 1 + math.ceil(600 / P.PRUNE_RECORD_LIMIT), passes)
        print(json.dumps({"g03_r1_prune_passes": len(passes),
                          "pruned": [r["delivery_prune"]["pruned_records"] for r in passes]}))

    # --- (c) cadence ---------------------------------------------------------
    def test_prune_follows_the_pass_due_gate_and_retention_lock(self):
        self.add("att-cand")
        for index in range(1, 7):
            self.put(1, index, "acked", 9 + index)
        with mock.patch.object(P, "PRUNE_RECORD_LIMIT", 2):
            at = self.now
            first = R.sweep(self.jobs, now_ns=at)
            self.assertEqual(first["delivery_prune"]["pruned_records"], 2)
            self.assertEqual(len(self.tombstones()), 2)
            self.assertEqual(R.sweep(self.jobs, now_ns=at), {"skipped": "not-due"})
            self.assertEqual(len(self.tombstones()), 2)
            fd = os.open(self.root / "homes/worker-retention/lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                busy = R.sweep(self.jobs, now_ns=at + 10 * R.DUE_SECONDS * 10**9)
            finally:
                os.close(fd)
            self.assertEqual(busy, {"skipped": "busy"})
            self.assertEqual(len(self.tombstones()), 2)
            later = R.sweep(self.jobs, now_ns=at + 10 * R.DUE_SECONDS * 10**9)
            self.assertEqual(later["delivery_prune"]["pruned_records"], 2)
            self.assertEqual(len(self.tombstones()), 4)

    def test_a_prune_failure_never_fails_the_home_pass(self):
        home = self.add("att-cand")
        with mock.patch.object(P, "prune_bounded", side_effect=RuntimeError("boom")):
            result = self.sweep(0)
        self.assertEqual(result["delivery_prune"], {"error": "RuntimeError"})
        self.assertEqual(result["examined"], 1)
        self.assertTrue(home.is_dir())

    # --- (d) same-pass detach --------------------------------------------------
    def _home_with_old_record(self):
        """A done HOME idle for two hours whose delivery intent names a
        9-day-old acked record (the `_delivery_state` path: a pruned record's
        tombstone time becomes that HOME's activity)."""
        digest, delivery = "a" * 64, "delivery-" + "b" * 32
        home = self.add("att-same-pass", parent_attempt_id="att-parent", delivery_intent="1",
                        delivery_id=delivery, delivery_recipient_digest=digest)
        for path in (home, *home.rglob("*")):
            os.utime(path, ns=(self.now - 2 * RETENTION_NS, self.now - 2 * RETENTION_NS))
        ended = datetime.fromtimestamp(self.now / 10**9 - 3 * 3600, timezone.utc)
        self.set_timestamp("att-same-pass", ended.isoformat().replace("+00:00", "Z"))
        record = self.root / "pending-delivery" / digest / f"{delivery}.json"
        self.delivery_record(record, "att-same-pass")
        stamp = time.time() - 9 * self.DAY
        os.utime(record, (stamp, stamp))
        return home, record

    def test_same_pass_detach_without_the_prune_is_due(self):
        """Control: with no prune this HOME is due in the very first pass."""
        home, record = self._home_with_old_record()
        with mock.patch.object(R, "_prune_delivery_records", return_value={}):
            result = self.sweep(0)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())
        self.assertTrue(record.exists())

    def test_same_pass_prune_restarts_the_idle_clock_and_never_deadlocks(self):
        """The prune runs before the HOME is judged; its tombstone is that
        HOME's newest activity, so the same pass keeps it (conservative) and a
        later pass removes it."""
        home, record = self._home_with_old_record()
        result = self.sweep(0)
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(result["delivery_prune"]["pruned_records"], 1)
        self.assertEqual(self.kept(result, "within-window"), 1, result)
        self.assertFalse(record.exists())
        self.assertTrue(P.tombstone_path(record).is_file())
        self.assertTrue(home.is_dir())
        self.assertEqual(self.sweep(8 / 7)["removed"], 1)
        self.assertFalse(home.exists())

    def test_a_record_naming_the_home_only_by_attempt_is_pruned_without_blocking_it(self):
        """The attempt-only relation carries no activity once the record is old,
        so pruning it neither protects nor endangers an already-due HOME."""
        home = self.add("att-same-pass")
        for path in (home, *home.rglob("*")):
            os.utime(path, ns=(self.now - 2 * RETENTION_NS, self.now - 2 * RETENTION_NS))
        ended = datetime.fromtimestamp(self.now / 10**9 - 3 * 3600, timezone.utc)
        self.set_timestamp("att-same-pass", ended.isoformat().replace("+00:00", "Z"))
        record = self.put(1, 1, "acked", 9, attempt="att-same-pass")
        result = self.sweep(0)
        self.assertEqual(result["delivery_prune"]["pruned_records"], 1)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())
        self.assertTrue(P.tombstone_path(record).is_file())


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
        with patch_split(DC, "_current_observer_is_host_like", return_value=False):
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
        with patch_split(DC, "_current_observer_is_host_like", return_value=True):
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

    def test_held_owner_or_live_provider_keep_home_but_ownerless_lockdir_does_not(self):
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
        # Intentional change: with no process naming the old lockdir, the home is released.
        self.sweep(0)
        self.sweep(8 / 7)
        self.assertFalse(provider_home.exists())

        live_home = self.add("att-summary-provider-live")
        _owner_lock, live_lock = self.attach_summary_owner("att-summary-provider-live", live_home)
        live_lock.parent.mkdir(parents=True, exist_ok=True)
        live_lock.mkdir()
        os.utime(live_lock, (old, old))
        self.start_provider(live_lock)
        self.assertEqual(self.kept(self.expire(), "summary-live"), 1)
        self.assertTrue(live_home.is_dir())

    def test_legacy_owner_state_without_runtime_home_is_not_a_conflict(self):
        home = self.add("att-legacy-owner")
        owner_lock, _provider_lock = self.attach_summary_owner("att-legacy-owner", home)
        state_path = Path(self.rows["att-legacy-owner"][1]["summary_state_file"])
        state = json.loads(state_path.read_text())
        del state["runtime_home"]
        state_path.write_text(json.dumps(state))
        owner_fd = os.open(owner_lock, os.O_RDWR)
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX)
            self.assertEqual(self.kept(self.expire(), "summary-live"), 1)
        finally:
            os.close(owner_fd)
        self.assertEqual(self.sweep(8 / 7 * 2)["removed"], 1)
        self.assertFalse(home.exists())

    def test_live_or_unverifiable_quiescence_keeps(self):
        live = self.add("att-q1")
        unknown = self.add("att-q2")
        real = DC.attempt_process_quiescence

        def fake(meta, *, terminal_receipt=False):
            if meta["attempt_id"] == "att-q1":
                return DC.ProcessQuiescence("live", "attempt-descendant-live")
            return DC.ProcessQuiescence("unverifiable", "process-namespace-unverifiable")

        with patch_split(DC, "attempt_process_quiescence", fake):
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
        with patch_split(DC, "attempt_process_quiescence", return_value=observed):
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
        with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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

    def test_unrelated_new_group_publishers_run_during_admission_and_next_due_pass_removes_home(self):
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
        # The candidate's reference admission was won before this unrelated writer
        # started, and no writer that can name the candidate can publish while it is
        # held. A new group made meanwhile is not evidence about the candidate, so
        # the same pass removes the HOME (the old root-metadata backstop deferred it
        # a whole pass); unrelated publication itself remains unblocked.
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

        def observed_relation(path, related, binding=None, **kwargs):
            result = real_relation(path, related, binding, **kwargs)
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

        try:
            with mock.patch.object(P.fcntl, "flock", observed), mock.patch.object(R, "_detach", detach):
                writer.start()
                self.assertTrue(parked.wait(5))
                result = self.sweep((8) / 7)
        finally:
            original_flock(held, fcntl.LOCK_UN)
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(path.read_text())["attempt_ids"], ["att-owner"])
        self.assertEqual(result["removed"], 0)
        self.assertGreaterEqual(result["deferred"] + self.kept(result, "late-evidence-changed"), 1)
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
                patch_split(DC, "attempt_process_quiescence", probe):
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
                        # Same shape as the producer: lockdir, then a process naming it.
                        provider_lock.parent.mkdir(parents=True, exist_ok=True)
                        provider_lock.mkdir()
                        self.start_provider(provider_lock)
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
        with VirtualDeliveries(self.root, self.now, count=R.MAX_DEPENDENCY_STAMPS + 2,
                               tombstones=True) as history:
            for number in range(20):
                result = R.sweep(self.jobs, now_ns=self.now + 2 * RETENTION_NS + number * 301 * 10**9)
                if result["removed"]:
                    break
            self.assertEqual(history.reads, 0)
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
            token=None,
            subject_id=candidate,
            base=base,
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
                {}, time.monotonic() + 2, mark=old_ns)

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
        with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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
                    patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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
                patch_split(DC, "_current_observer_is_host_like", return_value=True):
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
        with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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

        with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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

        with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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
        with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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
            with patch_split(DC, "_current_observer_is_host_like", return_value=True):
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
            with patch_split(DC, "_current_observer_is_host_like", return_value=True):
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
            with patch_split(DC, "_current_observer_is_host_like", return_value=True), \
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

    def test_opencode_shared_service_row_is_not_a_home_claim(self):
        home = self.add("att-shared-service", harness="opencode", declare=False,
                        runtime_home=R.SHARED_SERVICE_RUNTIME_HOME)
        self.assertEqual(self.sweep(0)["eligible"], 1)
        self.assertEqual(self.sweep(8 / 7)["removed"], 1)
        self.assertFalse(home.exists())
        # Only the exact token claims no home; any other relative value still conflicts.
        other = self.add("att-shared-other", harness="opencode", declare=False,
                         runtime_home="other-relative")
        result = self.expire()
        self.assertEqual(self.unknown(result, "runtime-home-conflict"), 1)
        self.assertTrue(other.is_dir())

    def test_freed_bytes_are_recorded_only_in_the_pass_result(self):
        home = self.add("att-freed")
        (home / "projects" / "big.bin").write_bytes(b"x" * 65536)
        self.assertEqual(self.sweep(0)["freed_bytes"], 0)
        result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 1)
        self.assertGreaterEqual(result["freed_bytes"], 65536)
        self.assertEqual(self.state()["last_result"]["freed_bytes"], result["freed_bytes"])
        self.assertEqual(self.sweep(9 / 7)["freed_bytes"], 0)

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
                patch_split(DC, "_current_observer_is_host_like", return_value=False):
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
        self.assertEqual(R.BACKLOG_DUE_SECONDS, 60)
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
        # Examination stops once the removal budget is full of due homes.
        self.assertEqual(result["due"], R.REMOVE_LIMIT)
        self.assertLess(R.REMOVE_LIMIT, due_count)
        self.assertEqual(result["more"], 1)
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
                    held_dependencies, under_lock = [], []
                    active = [False]
                    original_index, original_detach = R._delivery_index, R._detach
                    original_fresh, original_lock = R._fresh, R._registry_lock
                    # The hold is bounded by what runs under jobs.log.lock, not by a
                    # clock (0.25 s flaked at 0.398 s on a loaded runner): no pass that
                    # prepares or discovers history runs while it is held, the
                    # candidate's own rechecks there name a bounded set, and the final
                    # admission takes at most one stat-capped look at the recipient
                    # groups per hold.
                    history_passes = {name: getattr(R, name) for name in (
                        "_Snapshot", "_declared_index", "_candidate_recipient_groups",
                        "_late_delivery_change", "_prepare_candidate_delivery_baseline")}
                    held_related, held_looks = [], []
                    def outside(name, fn):
                        def checked(*args, **kwargs):
                            if active[0]:
                                under_lock.append(name)
                            return fn(*args, **kwargs)
                        return checked
                    original_fingerprint = R._reference_fingerprint
                    original_group_change = R._late_candidate_group_change
                    def fingerprint(root, identities, **kwargs):
                        if active[0]:
                            held_related.append(len(identities))
                        return original_fingerprint(root, identities, **kwargs)
                    def group_change(baseline, related, *args, **kwargs):
                        if active[0]:
                            held_related.append(len(related))
                        return original_group_change(baseline, related, *args, **kwargs)
                    original_scan = R._scan_published
                    def scan(*args, stat_limit=None, **kwargs):
                        if active[0]:
                            held_looks[-1].append(stat_limit)
                        return original_scan(*args, stat_limit=stat_limit, **kwargs)
                    def indexed(*args, **kwargs):
                        if active[0]:
                            under_lock.append("_delivery_index")
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
                        if active[0]:
                            held_dependencies.append(len(watched))
                        return original_fresh(watched)
                    @contextlib.contextmanager
                    def locked(jobs):
                        with original_lock(jobs) as admitted:
                            active[0] = True
                            held_looks.append([])
                            start = time.monotonic()
                            try:
                                yield admitted
                            finally:
                                holds.append(time.monotonic() - start)
                                active[0] = False
                    with contextlib.ExitStack() as patches:
                        for name, fn in history_passes.items():
                            patches.enter_context(mock.patch.object(R, name, outside(name, fn)))
                        patches.enter_context(mock.patch.object(R, "_delivery_index", indexed))
                        patches.enter_context(mock.patch.object(R, "_reference_fingerprint", fingerprint))
                        patches.enter_context(mock.patch.object(R, "_late_candidate_group_change", group_change))
                        patches.enter_context(mock.patch.object(R, "_scan_published", scan))
                        patches.enter_context(mock.patch.object(R, "_detach", detached))
                        patches.enter_context(mock.patch.object(R, "_fresh", fresh))
                        patches.enter_context(mock.patch.object(R, "_registry_lock", locked))
                        for invocation in range(40):
                            result = R.sweep(fixture.jobs, now_ns=fixture.now + int(8 / 7 * RETENTION_NS)
                                             + invocation * 301 * 10**9)
                            if result["removed"] == 3:
                                break
                    self.assertEqual(result["removed"], 3, (kind, result))
                    self.assertTrue(all(not home.exists() for home in homes))
                    self.assertEqual(len(preparation), 3)
                    self.assertEqual(len(holds), 6)
                    self.assertLess(max(dependencies), 512)
                    self.assertEqual(under_lock, [], (kind, under_lock))
                    self.assertTrue(held_dependencies, kind)
                    self.assertLess(max(held_dependencies), 512)
                    self.assertLess(max(held_related, default=0), 512)
                    self.assertEqual(sum(map(len, held_looks)), 3, held_looks)
                    self.assertTrue(all(len(looks) <= 1 for looks in held_looks), held_looks)
                    self.assertTrue(all(limit is not None for looks in held_looks for limit in looks),
                                    held_looks)
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
        # Examination stops once the removal budget is full, so nothing is
        # examined-then-deferred and the rest wait for the next pass.
        self.assertEqual(first["examined"], R.REMOVE_LIMIT)
        self.assertEqual(self.kept(first, "deferred"), 0)
        second = self.sweep(8.1 / 7)
        self.assertEqual(second["removed"], 3)
        self.assertFalse(any(path.exists() for path in homes))
        self.assertEqual(self.sweep(8.2 / 7)["removed"], 0)

    def test_cursor_resumes_at_due_homes_the_pass_could_not_remove(self):
        # More due homes than one pass removes, and more homes than one
        # examine batch: the next pass must continue right after the last
        # examined home instead of skipping the rest of a whole cycle.
        attempts = [f"att-r{index}" for index in range(R.EXAMINE_LIMIT + R.REMOVE_LIMIT + 4)]
        paths = {attempt: self.add(attempt) for attempt in attempts}
        self.sweep(0)
        first = self.sweep(8 / 7)
        self.assertEqual((first["removed"], first["examined"]), (R.REMOVE_LIMIT, R.REMOVE_LIMIT))
        remaining = len(attempts) - R.REMOVE_LIMIT
        total = R.REMOVE_LIMIT
        for step in range(1, 12):
            result = self.sweep((8 + step / 1000) / 7)
            self.assertLessEqual(result["removed"], R.REMOVE_LIMIT)
            total += result["removed"]
            if not any(path.exists() for path in paths.values()):
                break
        self.assertEqual(total, len(attempts))
        self.assertLessEqual(step, -(-remaining // R.REMOVE_LIMIT))

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

    def test_due_homes_that_detach_refuses_do_not_keep_the_short_interval(self):
        # RV-1041 R2: a full budget of due homes that `_detach` refuses again
        # (renewed protection, unsafe partial retention) is not backlog.
        for index in range(R.REMOVE_LIMIT + 1):
            self.add(f"att-refused-{index}")
        base = self.now + int(8 / 7 * RETENTION_NS)
        R.sweep(self.jobs, now_ns=self.now, force=True)
        with mock.patch.object(R, "_detach", return_value="defer:partial-retention-unsafe"):
            refused = R.sweep(self.jobs, now_ns=base, force=True)
        self.assertEqual((refused["due"], refused["removed"]), (R.REMOVE_LIMIT, 0))
        self.assertNotIn("more", refused)
        self.assertTrue(all(self.home_path(f"att-refused-{index}").exists()
                            for index in range(R.REMOVE_LIMIT + 1)))

    def test_next_pass_starts_sooner_only_while_due_homes_are_left_behind(self):
        for index in range(R.REMOVE_LIMIT + 1):
            self.add(f"att-more-{index}")
        base = self.now + int(8 / 7 * RETENTION_NS)
        R.sweep(self.jobs, now_ns=self.now, force=True)
        first = R.sweep(self.jobs, now_ns=base, force=True)
        self.assertEqual((first["removed"], first.get("more")), (R.REMOVE_LIMIT, 1))
        gap = R.BACKLOG_DUE_SECONDS * 10**9
        self.assertEqual(R.sweep(self.jobs, now_ns=base + gap - 10**9), {"skipped": "not-due"})
        second = R.sweep(self.jobs, now_ns=base + gap)
        self.assertEqual(second["removed"], 1)
        self.assertNotIn("more", second)
        # Nothing is left behind now, so the ordinary interval applies again.
        self.assertEqual(R.sweep(self.jobs, now_ns=base + gap + 2 * gap),
                         {"skipped": "not-due"})
        self.assertNotIn("skipped", R.sweep(self.jobs, now_ns=base + gap + R.DUE_SECONDS * 10**9))

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
        # The outside() checks above are the bound on the hold; no wall clock.
        self.assertEqual(len(held), 2)

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



class G03(Fixture):
    def test_g03_pinned_and_current_publisher_first_after_resumed_scan(self):
        for pinned in (True, False):
            for new_group in (False, True):
                for relation in ("attempt", "parent"):
                    with self.subTest(pinned=pinned, new_group=new_group, relation=relation):
                        case = Fixture(); case.setUp()
                        try:
                            attempt = "att-g03-final-publication"
                            home = case.add(attempt, harness="codex")
                            recipients = ["g03-a", "g03-b", "g03-c"]
                            with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS,
                                    groups=3, recipient_keys=recipients):
                                at = case.now + 2 * RETENTION_NS
                                first = R.sweep(case.jobs, now_ns=at)
                                self.assertGreater(first["deferred"], 0)
                                before = P.reference_token(case.root, attempt)
                                original, published = R._late_candidate_delivery_change, []
                                recipient = "g03-new" if new_group else recipients[0]
                                def publish_then_check(ctx, selected, deadline, binding=None):
                                    if not published:
                                        self.assertNotIn(attempt, AR.held_attempts())
                                        with normal_delivery_writer(case.root, recipient, attempt,
                                                pinned=pinned, relation=relation):
                                            pass
                                        published.append(True)
                                    return original(ctx, selected, deadline, binding)
                                with mock.patch.object(R, "_late_candidate_delivery_change", publish_then_check):
                                    result = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                                self.assertEqual(result["removed"], 0, result)
                                self.assertTrue(home.exists())
                                self.assertEqual(published, [True])
                                self.assertEqual(before == P.reference_token(case.root, attempt), pinned)
                                record = P.read(case.root, recipient, "delivery-" + "f" * 32)
                                self.assertEqual(record["state"], "pending")
                                print(json.dumps({"g03_home_publisher_first": True, "pinned": pinned,
                                    "new_group": new_group, "relation": relation, "result": result}))
                        finally:
                            case.doCleanups()

    def test_g03_pinned_and_current_deletion_first_fences_home_rename(self):
        for pinned in (True, False):
            for new_group in (False, True):
                for relation in ("attempt", "parent"):
                    with self.subTest(pinned=pinned, new_group=new_group, relation=relation):
                        case = Fixture(); case.setUp()
                        try:
                            attempt = "att-g03-home-delete-first"
                            home = case.add(attempt, harness="codex")
                            recipients = ["g03-a", "g03-b", "g03-c"]
                            with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS,
                                    groups=3, recipient_keys=recipients):
                                at = case.now + 2 * RETENTION_NS
                                self.assertGreater(R.sweep(case.jobs, now_ns=at)["deferred"], 0)
                                recipient = "g03-new" if new_group else recipients[0]
                                real_rename, renamed = os.rename, []
                                with normal_delivery_writer(case.root, recipient, attempt,
                                        pinned=pinned, relation=relation, gated=True) as child:
                                    def rename(name, target, **kwargs):
                                        if name == home.name:
                                            child.stdin.write("go\n"); child.stdin.flush()
                                            wait_for_reference_attempt(child)
                                            self.assertIsNone(child.poll())
                                            result = real_rename(name, target, **kwargs)
                                            self.assertIsNone(child.poll(), "publisher passed HOME rename")
                                            renamed.append(True)
                                            return result
                                        return real_rename(name, target, **kwargs)
                                    with case.replaced("rename", rename):
                                        result = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                                self.assertEqual(renamed, [True])
                                self.assertEqual(result["removed"], 1, result)
                                self.assertFalse(home.exists())
                                self.assertEqual(P.read(case.root, recipient, "delivery-" + "f" * 32)["state"], "pending")
                        finally:
                            case.doCleanups()

    def test_g03_idempotent_unrelated_create_preserves_multi_pass_progress(self):
        for groups, writer_group in ((1, 0), (3, 0), (3, 2)):
            with self.subTest(groups=groups, writer_group=writer_group):
                case = Fixture(); case.setUp()
                try:
                    free = case.add("att-g03-cadence-free", harness="codex")
                    opened = case.add("att-g03-cadence-open", harness="codex")
                    parent = case.add("att-g03-cadence-parent", harness="codex")
                    recipients = [f"g03-cadence-{i}" for i in range(groups)]
                    with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS, groups=groups,
                            recipient_keys=recipients,
                            open_refs=[(["att-g03-cadence-open"], "att-g03-cadence-parent")]):
                        recipient = recipients[writer_group]
                        def noop():
                            case.publish_delivery("delivery-" + "f" * 32, recipient, ["att-unrelated"], "-")
                        noop()
                        stamps = [R._stamp(case.root / "pending-delivery"),
                                  R._stamp(P.record_directory(case.root, recipient))]
                        passes = []
                        for i in range(3):
                            noop()
                            self.assertEqual(stamps, [R._stamp(case.root / "pending-delivery"),
                                                     R._stamp(P.record_directory(case.root, recipient))])
                            result = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS + i * 301 * 10**9)
                            passes.append(result)
                            self.assertLessEqual(result["delivery_reads"], R.DELIVERY_READ_BUDGET)
                            self.assertEqual(result["unknown"], {})
                            self.assertTrue(opened.exists() and parent.exists())
                            if not free.exists():
                                break
                        self.assertEqual(len(passes), 2, passes)
                        self.assertGreater(passes[0]["deferred"], 0)
                        self.assertEqual(passes[1]["removed"], 1, passes)
                        self.assertLess(passes[1]["delivery_reads"], 32)
                        print(json.dumps({"g03_home_cadence": groups, "writer_group": writer_group, "passes": passes}))
                finally:
                    case.doCleanups()

    def test_g03_large_history_late_open_candidate_progress_across_adapters(self):
        self.assertEqual(R.DELIVERY_READ_BUDGET, 4096)
        for groups in (1, 3):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(groups=groups, harness=harness):
                    case = Fixture(); case.setUp()
                    try:
                        free = case.add("att-g03-free", harness=harness)
                        child = case.add("att-g03-child", harness=harness)
                        parent = case.add("att-g03-parent", harness=harness)
                        with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS,
                                groups=groups, open_refs=[(["att-g03-child"], "att-g03-parent")]) as history:
                            at = case.now + 2 * RETENTION_NS
                            first = R.sweep(case.jobs, now_ns=at)
                            self.assertEqual((first["eligible"], first["removed"]), (0, 0), first)
                            self.assertGreater(first["deferred"], 0)
                            self.assertEqual(first["unknown"], {})
                            self.assertEqual(first["kept"].get("delivery-open", 0), 0,
                                             "the open reference must be beyond the first page")
                            first_reads = history.reads
                            second = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                            self.assertEqual(second["removed"], 1, second)
                            self.assertFalse(free.exists())
                            self.assertTrue(child.exists() and parent.exists())
                            self.assertLessEqual(first_reads, R.DELIVERY_READ_BUDGET)
                            self.assertEqual(history.reads, 4098)
                            self.assertLessEqual(second["delivery_reads"], R.DELIVERY_READ_BUDGET)
                            state = case.state()["delivery_observation"]
                            self.assertNotRegex(json.dumps(state), r"delivery-[0-9a-f]{32}")
                            print(json.dumps({"g03": harness, "groups": groups,
                                "reads_by_pass": [first_reads, history.reads - first_reads],
                                "entries_by_pass": [first["delivery_entries"], second["delivery_entries"]],
                                "state_bytes": len(json.dumps(state)), "removed": second["removed"]}))
                    finally:
                        case.doCleanups()

    def test_g03_membership_inode_and_mode_changes_still_invalidate_resume(self):
        """A replaced store or a changed in-progress group is read again; a group
        added later or a mode correction does not restart the traversal, and a
        record naming the HOME still protects it (its writer moved the token)."""
        for mutation in ("new-group", "new-record", "root-inode", "group-inode", "mode-correction"):
            with self.subTest(mutation=mutation):
                case = Fixture(); case.setUp()
                try:
                    home = case.add("att-g03-generation", harness="codex")
                    recipients = ["g03-generation-a", "g03-generation-b", "g03-generation-c"]
                    with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS,
                            groups=3, recipient_keys=recipients) as history:
                        base = case.root / "pending-delivery"
                        case.publish_delivery("delivery-" + "f" * 32, recipients[2], ["att-unrelated"], "-")
                        at = case.now + 2 * RETENTION_NS
                        first = R.sweep(case.jobs, now_ns=at)
                        self.assertGreater(first["deferred"], 0)
                        # Groups are walked in name order; the last one is still in progress.
                        current = case.state()["delivery_observation"]["position"]["group"]
                        in_progress = next(key for key in recipients if P.recipient_digest(key) == current)
                        if mutation == "new-group":
                            case.publish_delivery("delivery-" + "e" * 32, "g03-added-group", ["att-g03-generation"], "-")
                        elif mutation == "new-record":
                            case.publish_delivery("delivery-" + "e" * 32, in_progress, ["att-g03-generation"], "-")
                        elif mutation == "root-inode":
                            base.rename(case.root / "old-deliveries")
                            for group in history.paths:
                                group.mkdir(parents=True)
                        elif mutation == "group-inode":
                            group = base / current
                            group.rename(case.root / "old-group"); group.mkdir()
                        else:
                            os.chmod(base, 0o750)
                            case.publish_delivery("delivery-" + "f" * 32, recipients[2], ["att-unrelated"], "-")
                            self.assertEqual(__import__("stat").S_IMODE(base.stat().st_mode), 0o700)
                        second = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                        self.assertEqual(second["unknown"], {}, second)
                        if mutation in ("new-group", "mode-correction"):
                            # Stable no-op cadence: the traversal resumes, no reread.
                            self.assertLess(second["delivery_reads"], 32, second)
                        else:
                            # A genuine changed generation must reread its directory.
                            self.assertGreater(second["delivery_reads"], 1300, second)
                        if mutation in ("new-group", "new-record"):
                            self.assertEqual(second["removed"], 0, second)
                            self.assertTrue(home.exists())
                        print(json.dumps({"g03_generation_mutation": mutation, "result": second}))
                finally:
                    case.doCleanups()

    def test_g03_ack_and_prune_keep_related_home_and_unrelated_resume_progress(self):
        for action in ("ack", "prune"):
            with self.subTest(action=action):
                case = Fixture(); case.setUp()
                try:
                    free = case.add("att-g03-mutation-free", harness="codex")
                    related = case.add("att-g03-mutation-related", harness="codex")
                    recipients = ["g03-mutation-a", "g03-mutation-b", "g03-mutation-c"]
                    # Groups are read in name order: mutate one the first pass finished.
                    recipients.sort(key=P.recipient_digest)
                    with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS,
                            groups=3, recipient_keys=recipients):
                        delivery = "delivery-" + "f" * 32
                        case.publish_delivery(delivery, recipients[0], ["att-g03-mutation-related"], "-")
                        P.claim(case.root, recipients[0], delivery, claim_owner="g03", lease_seconds=30)
                        at = case.now + 2 * RETENTION_NS
                        first = R.sweep(case.jobs, now_ns=at)
                        self.assertEqual(first["removed"], 0)
                        self.assertGreater(first["deferred"], 0)
                        base_stamp = R._stamp(case.root / "pending-delivery")
                        token = P.reference_token(case.root, "att-g03-mutation-related")
                        P.ack(case.root, recipients[0], delivery, acked_by="g03")
                        if action == "prune":
                            pruned = P.prune(case.root, apply=True, now=time.time() + 1, retention_seconds=0)
                            self.assertEqual(pruned["pruned_records"], 1, pruned)
                            self.assertTrue(Path(str(P.record_path(case.root, recipients[0], delivery)) + ".pruned").exists())
                        self.assertNotEqual(token, P.reference_token(case.root, "att-g03-mutation-related"))
                        self.assertEqual(base_stamp, R._stamp(case.root / "pending-delivery"))
                        second = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                        self.assertEqual(second["removed"], 1, second)
                        self.assertLess(second["delivery_reads"], 32)
                        self.assertFalse(free.exists())
                        self.assertTrue(related.exists(), "changed related evidence must remain conservative")
                        print(json.dumps({"g03_interpass_mutation": action, "result": second}))
                finally:
                    case.doCleanups()

    def test_g03_unknown_is_protected_and_partial_keeps_established_clock(self):
        home = self.add("att-g03-clock")
        self.set_timestamp("att-g03-clock", "unparseable")
        self.sweep(0)
        first_clock = self.state()["eligible_since"][home.name]
        with VirtualDeliveries(self.root, self.now - 3 * RETENTION_NS, malformed=True):
            at = self.now + 2 * RETENTION_NS
            partial = R.sweep(self.jobs, now_ns=at)
            self.assertEqual(partial["unknown"], {})
            self.assertEqual(self.state()["eligible_since"][home.name], first_clock)
            last = R.sweep(self.jobs, now_ns=at + 301 * 10**9)
            self.assertEqual(last["unknown"].get("delivery-index-unknown"), 1)
            self.assertTrue(home.exists())
            self.assertEqual(self.state()["eligible_since"][home.name], first_clock)

    def test_g03_resumes_virtual_history_in_another_process(self):
        free = self.add("att-g03-restart-free", harness="codex")
        related = self.add("att-g03-restart-open", harness="codex")
        at = self.now + 2 * RETENTION_NS
        with VirtualDeliveries(self.root, self.now - 3 * RETENTION_NS,
                              open_refs=[(["att-g03-restart-open"], "-")]) as history:
            first = R.sweep(self.jobs, now_ns=at)
            first_reads = history.reads
        self.assertGreater(first["deferred"], 0)
        code = """import importlib.util,sys,json
from pathlib import Path
spec=importlib.util.spec_from_file_location('g03_fixture',sys.argv[1]); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(sys.argv[2]); now=int(sys.argv[3]);at=int(sys.argv[4])
with m.VirtualDeliveries(root,now-3*m.RETENTION_NS,open_refs=[(['att-g03-restart-open'],'-')]) as h:
 result=m.R.sweep(root/'jobs.log',now_ns=at)
 print(json.dumps({'result':result,'reads':h.reads}))
"""
        child = subprocess.run([sys.executable, "-B", "-c", code, __file__, str(self.root),
                                str(self.now), str(at + 301 * 10**9)],
                               capture_output=True, text=True, check=True)
        resumed = json.loads(child.stdout)
        self.assertEqual(resumed["result"]["removed"], 1, resumed)
        self.assertEqual(first_reads + resumed["reads"], 4098)
        self.assertTrue(related.exists()); self.assertFalse(free.exists())
        print(json.dumps({"g03_process_restart": True, "reads": [first_reads, resumed["reads"]]}))

    def test_g03_real_directory_cookie_restart(self):
        directory = self.root / "cookie-fixture"
        directory.mkdir()
        for i in range(12):
            (directory / f"file-{i}").touch()
        cookie, seen = 0, []
        code = """import sys,json,time
sys.path.insert(0,sys.argv[1]);import worker_home_retention as r
from pathlib import Path
print(json.dumps(r._directory_page(Path(sys.argv[2]),int(sys.argv[3]),3,time.monotonic()+5)[:2]))
"""
        for _ in range(10):
            child = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT/'utilities'),
                                    str(directory), str(cookie)], capture_output=True, text=True, check=True)
            entries, eof = json.loads(child.stdout)
            for name, cookie, _dtype in entries:
                if name not in (".", ".."):
                    seen.append(name)
            if eof:
                break
        self.assertEqual(set(seen), {f"file-{i}" for i in range(12)})
        self.assertEqual(len(seen), 12)

    def test_g03_supported_root_bindings_resume_guarded_inventory_in_separate_processes(self):
        for kind in ("home", "xdg", "harness", "explicit", "inherited", "activation"):
            with self.subTest(binding=kind):
                owned = self.root / kind
                env = {"HOME": str(owned / "home")}
                agent = owned / "checkout"
                explicit = None
                if kind == "xdg":
                    env["XDG_STATE_HOME"] = str(owned / "xdg")
                elif kind == "harness":
                    env["HARNESS_STATE_ROOT"] = str(owned / "state")
                elif kind == "explicit":
                    explicit = str(owned / "selected/jobs.log")
                elif kind == "inherited":
                    env["AGENT_DISPATCH_JOBS"] = str(owned / "inherited/jobs.log")
                elif kind == "activation":
                    agent = owned / "runtime/.harness/bundles/fixture/source"
                selected = DC.resolve_global_registry(agent, explicit, 1, "start", environ=env)
                root = DC.resolve_dispatch_state_root(agent, explicit, environ=env)
                self.assertEqual(root, selected.path.parent)
                self.assertEqual(root, R._state_root(selected.path))
                # Exercise initialization too, without using the real user's
                # state-root binding or changing an operational location.
                with mock.patch.dict(os.environ, env, clear=True):
                    DC.ensure_global_registry_writable(selected.path)
                recipient = "g03-root-binding"
                for i in range(3):
                    receipt = {"schema_version": 2, "fixture": "g03-root-binding"}
                    P.create(root, recipient_kind="codex-native-queue", recipient_key=recipient,
                             delivery_id=f"delivery-{i:032x}", session_generation="",
                             session_generation_supported="0", attempt_ids=[f"att-root-{i}"],
                             parent_attempt_id="-", route_id="rt-root", route_node="execute",
                             receipt=receipt, receipt_digest=P._canonical_receipt_digest(receipt), row_revisions={})
                directory = P.record_directory(root, recipient)
                expected = {p.name for p in directory.iterdir()}
                cookie, seen, pages, generation = 0, [], [], None
                code = """import sys,json,time
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import dispatch_contract as dc,worker_home_retention as r
agent,explicit,env,root=json.loads(sys.argv[4])
assert dc.resolve_dispatch_state_root(Path(agent),explicit,environ=env)==Path(root)
print(json.dumps(r._directory_page(Path(sys.argv[2]),int(sys.argv[3]),3,time.monotonic()+5)))
"""
                binding = json.dumps([str(agent), explicit, env, str(root)])
                for _ in range(12):
                    child = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT / "utilities"),
                                            str(directory), str(cookie), binding],
                                           capture_output=True, text=True, check=True, timeout=10)
                    entries, eof, stamp = json.loads(child.stdout)
                    if generation is None:
                        generation = stamp
                    self.assertEqual(stamp, generation)
                    pages.append(len(entries))
                    for name, cookie, _dtype in entries:
                        if name not in (".", ".."):
                            seen.append(name)
                    if eof:
                        break
                self.assertTrue(eof)
                self.assertEqual(set(seen), expected)
                self.assertEqual(len(seen), len(expected))
                self.assertEqual(directory.stat().st_uid, os.geteuid())
                for path in directory.glob("*.json"):
                    value, _ = R._read_json(str(path), R.MAX_DELIVERY_BYTES)
                    self.assertTrue(P.record_references(value, group=directory.name, delivery_id=path.name[:-5]))
                print(json.dumps({"g03_guarded_root_binding": kind, "pages": pages,
                                  "inventory_entries": len(seen), "uid": generation[3], "euid": os.geteuid()}))

    def test_g03_recent_terminal_activity_and_identityless_open(self):
        recent = self.add("att-g03-recent")
        legacy = self.add_unmapped_legacy("att-g03-legacy")
        free = self.add_unmapped_legacy("att-g03-legacy-free")
        with VirtualDeliveries(self.root, self.now, count=17,
                terminal_ref="att-g03-recent", open_refs=[(["att-g03-legacy"], "-")]), \
                mock.patch.object(R._Context, "home_process_evidence", return_value=("absent", "fixture")):
            first = self.sweep(0)
            self.assertEqual(first["kept"].get("delivery-open"), 1, first)
            self.assertTrue(legacy.exists())
            second = self.sweep(2)
            self.assertTrue(legacy.exists()); self.assertFalse(free.exists())
            self.assertFalse(recent.exists())
            self.assertEqual(second["removed"], 2, second)


    def test_g03_deadline_pages_resume_and_preserve_clock(self):
        home = self.add("att-g03-deadline")
        self.set_timestamp("att-g03-deadline", "invalid")
        self.sweep(0)
        established = self.state()["eligible_since"][home.name]
        with VirtualDeliveries(self.root, self.now - 4 * RETENTION_NS, count=41) as history:
            read = history.read
            clock = [0.0]
            def timed_read(*args, **kwargs):
                value = read(*args, **kwargs)
                if history._virtual(args[0]):
                    clock[0] += 1.0
                return value
            with mock.patch.object(R, "_read_json", timed_read), \
                    mock.patch.object(R.time, "monotonic", side_effect=lambda: clock[0]):
                counts = []
                for i in range(8):
                    clock[0] = 0.0
                    before = history.reads
                    result = R.sweep(self.jobs, now_ns=self.now + 2 * RETENTION_NS + i * 301 * 10**9)
                    counts.append(history.reads - before)
                    self.assertEqual(result["unknown"], {})
                    if result["removed"]:
                        break
                    self.assertEqual(self.state()["eligible_since"][home.name], established)
                    self.assertGreater(result["deferred"], 0)
                self.assertEqual(result["removed"], 1, result)
                self.assertEqual(sum(counts), 41)
                self.assertTrue(all(n <= 10 for n in counts), counts)
                print(json.dumps({"g03_deadline_reads": counts, "established_clock_preserved": True}))

    def test_g03_final_admission_does_not_scan_history_or_processes(self):
        home = self.add("att-g03-critical", harness="codex")
        with VirtualDeliveries(self.root, self.now - 3 * RETENTION_NS) as history:
            at = self.now + 2 * RETENTION_NS
            R.sweep(self.jobs, now_ns=at)
            active = [False]
            real_lock, real_page, real_read = R._registry_lock_until, R._directory_page, R._read_json
            real_probe = DC.attempt_process_quiescence
            @contextlib.contextmanager
            def lock(*args):
                with real_lock(*args) as admitted:
                    active[0] = True
                    try:
                        yield admitted
                    finally:
                        active[0] = False
            def page(*args):
                self.assertFalse(active[0], "history enumeration under registry admission")
                return real_page(*args)
            def read(path, *args, **kwargs):
                if history._virtual(path):
                    self.assertFalse(active[0], "history body under registry admission")
                return real_read(path, *args, **kwargs)
            def probe(*args, **kwargs):
                self.assertFalse(active[0], "process census under registry admission")
                return real_probe(*args, **kwargs)
            with mock.patch.object(R, "_registry_lock_until", lock), \
                    mock.patch.object(R, "_directory_page", page), mock.patch.object(R, "_read_json", read), \
                    patch_split(DC, "attempt_process_quiescence", probe):
                result = R.sweep(self.jobs, now_ns=at + 301 * 10**9)
            self.assertEqual(result["removed"], 1, result)
            self.assertFalse(home.exists())

    def test_g03_corrupt_progress_and_token_reobserve_automatically(self):
        for fault in ("missing", "torn", "cohort", "root", "cookie", "token", "inode",
                      "mark-torn", "mark-forged", "mark-future", "mark-missing"):
            with self.subTest(fault=fault):
                case = Fixture(); case.setUp()
                try:
                    home = case.add("att-g03-state", harness="codex")
                    with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS, count=7), \
                            mock.patch.object(R, "DELIVERY_READ_BUDGET", 5):
                        at = case.now + 2 * RETENTION_NS
                        first = R.sweep(case.jobs, now_ns=at)
                        self.assertEqual(first["removed"], 0)
                        path = case.root / "homes/worker-retention/state.json"
                        state = case.state()
                        if fault == "missing": path.unlink()
                        elif fault == "torn": path.write_text('{"delivery_observation":')
                        elif fault == "token": AR.lock_path(case.root, "att-g03-state").write_bytes(b"torn")
                        elif fault == "inode":
                            group = case.root / "pending-delivery" / f"{1:064x}"
                            group.rename(case.root / "old-group"); group.mkdir()
                        elif fault.startswith("mark-"):
                            obs = state["delivery_observation"]
                            self.assertIsInstance(obs["mark"], int)
                            if fault == "mark-torn": obs["mark"] = "torn"
                            if fault == "mark-forged": obs["mark"] = time.time_ns() - 1
                            if fault in ("mark-future", "mark-missing"):
                                # A consistent record that still cannot be trusted: re-sealed.
                                if fault == "mark-future": obs["mark"] = time.time_ns() + 86400 * 10**9
                                else: del obs["mark"]
                                obs["integrity"] = R._generation({k: v for k, v in obs.items() if k != "integrity"})
                            path.write_text(json.dumps(state))
                        else:
                            obs = state["delivery_observation"]
                            if fault == "cohort": obs["identity"] = "foreign"
                            if fault == "root": obs["position"]["root_generation"] = "foreign"
                            if fault == "cookie": obs["position"]["cookie"] = "torn"
                            path.write_text(json.dumps(state))
                        real_advance, resumed = R._DeliveryObservation.advance, []

                        def advance(self_):
                            resumed.append(self_.resumed)
                            return real_advance(self_)
                        with mock.patch.object(R._DeliveryObservation, "advance", advance):
                            for i in range(1, 12):
                                result = R.sweep(case.jobs, now_ns=at + i * 301 * 10**9)
                                if result["removed"]:
                                    break
                        if fault.startswith("mark-"):
                            # An untrusted mark is never resumed; the cohort is observed afresh.
                            self.assertEqual(resumed[:1], [False], (fault, resumed))
                        self.assertEqual(result["removed"], 1, result)
                        self.assertFalse(home.exists())
                finally:
                    case.doCleanups()


    def test_g03_pending_cohort_finishes_while_another_protection_is_open(self):
        homes = {a: self.add(a, harness="codex") for a in ("att-g03-rotate-a", "att-g03-rotate-b")}
        with VirtualDeliveries(self.root, self.now - 3 * RETENTION_NS, count=17), \
                mock.patch.object(R, "DELIVERY_READ_BUDGET", 5), mock.patch.object(R, "EXAMINE_LIMIT", 1):
            at = self.now + 2 * RETENTION_NS
            first = R.sweep(self.jobs, now_ns=at)
            self.assertGreater(first["deferred"], 0)
            pending_key = self.state()["delivery_observation"]["subjects"][0]
            pending = next(a for a, home in homes.items() if home.name == pending_key)
            other = next(a for a in homes if a != pending)
            self.set_status(pending, "open")
            for i in range(1, 20):
                result = R.sweep(self.jobs, now_ns=at + i * 301 * 10**9)
                self.assertLessEqual(result["examined"], 1)
                if not homes[other].exists():
                    break
            self.assertFalse(homes[other].exists(), result)
            self.assertTrue(homes[pending].exists())


    def test_g03_journal_rename_and_partial_tree_interruptions_resume_normally(self):
        class Interrupted(BaseException):
            pass
        for phase in ("journal", "rename", "tree"):
            with self.subTest(phase=phase):
                case = Fixture(); case.setUp()
                try:
                    attempt = "att-g03-interrupted-" + phase
                    home = case.add(attempt, harness="codex")
                    key = home.name
                    real_rename, real_unlink = os.rename, os.unlink
                    def rename(name, target, **kwargs):
                        if name == key and phase == "journal":
                            raise Interrupted()
                        result = real_rename(name, target, **kwargs)
                        if name == key and phase == "rename":
                            raise Interrupted()
                        return result
                    def unlink(name, **kwargs):
                        result = real_unlink(name, **kwargs)
                        if name == "transcript.jsonl" and phase == "tree":
                            raise Interrupted()
                        return result
                    at = case.now + 2 * RETENTION_NS
                    with case.replaced("rename", rename), case.replaced("unlink", unlink):
                        with self.assertRaises(Interrupted):
                            R.sweep(case.jobs, now_ns=at)
                    retiring = case.root / "homes/worker-retention/retiring"
                    self.assertTrue(list(retiring.glob("*.journal")))
                    if phase == "journal":
                        case.publish_delivery("delivery-" + "e" * 32, "g03-interrupted-parent", [attempt], "-")
                    else:
                        home.mkdir()
                        (home / "new-writer").write_text("keep replacement inode")
                    result = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                    self.assertTrue(home.exists(), result)
                    if phase != "journal":
                        self.assertEqual((home / "new-writer").read_text(), "keep replacement inode")
                        self.assertEqual(result["detached_leftover"], 1, result)
                    self.assertFalse(list(retiring.iterdir()))
                finally:
                    case.doCleanups()


    def test_g03_related_terminal_activity_uses_same_idle_clock_across_adapters(self):
        for harness in ("claude", "codex", "opencode"):
            for state in ("acked", "expired", "rejected"):
                with self.subTest(harness=harness, state=state):
                    case = Fixture(); case.setUp()
                    try:
                        related = case.add("att-g03-terminal", harness=harness)
                        free = case.add("att-g03-terminal-free", harness=harness)
                        old = case.now - 3 * RETENTION_NS
                        for attempt, home in (("att-g03-terminal", related), ("att-g03-terminal-free", free)):
                            case.set_timestamp(attempt, datetime.fromtimestamp(old / 10**9, timezone.utc).isoformat())
                            os.utime(home, ns=(old, old))
                        with VirtualDeliveries(case.root, case.now, count=1,
                                terminal_ref="att-g03-terminal", terminal_state=state):
                            first = R.sweep(case.jobs, now_ns=case.now)
                            self.assertEqual(first["removed"], 1, first)
                            self.assertFalse(free.exists()); self.assertTrue(related.exists())
                            before_hour = R.sweep(case.jobs, now_ns=case.now + RETENTION_NS - 1)
                            self.assertEqual(before_hour["removed"], 0)
                            after_hour = R.sweep(case.jobs, now_ns=case.now + RETENTION_NS + 301 * 10**9)
                            self.assertEqual(after_hour["removed"], 1, after_hour)
                    finally:
                        case.doCleanups()


    def test_g03_numeric_cookie_corruption_cannot_skip_a_late_open_reference(self):
        free = self.add("att-g03-corrupt-free", harness="codex")
        opened = self.add("att-g03-corrupt-open", harness="codex")
        with VirtualDeliveries(self.root, self.now - 3 * RETENTION_NS, count=7,
                open_refs=[(["att-g03-corrupt-open"], "-")]), mock.patch.object(R, "DELIVERY_READ_BUDGET", 5):
            at = self.now + 2 * RETENTION_NS
            first = R.sweep(self.jobs, now_ns=at)
            self.assertEqual(first["removed"], 0)
            state = self.state()
            state["delivery_observation"]["position"]["cookie"] = 8  # valid integer, wrong opaque position
            (self.root / "homes/worker-retention/state.json").write_text(json.dumps(state))
            partial = R.sweep(self.jobs, now_ns=at + 301 * 10**9)
            self.assertEqual(partial["removed"], 0, partial)
            self.assertGreater(partial["delivery_reads"], 0, "damaged progress needs cold observation")
            for i in range(2, 9):
                result = R.sweep(self.jobs, now_ns=at + i * 301 * 10**9)
                self.assertTrue(opened.exists(), result)
                if not free.exists():
                    break
            self.assertFalse(free.exists(), result)
            self.assertTrue(opened.exists())


    def test_g03_many_recipient_groups_resume_without_per_candidate_history(self):
        homes = [self.add(f"att-g03-many-groups-{i}", harness="codex") for i in range(3)]
        with VirtualRecipientGroups(self.root, 32769) as history:
            counts = []
            for invocation in range(40):
                result = R.sweep(self.jobs, now_ns=self.now + 2 * RETENTION_NS + invocation * 301 * 10**9)
                counts.append(result["delivery_entries"])
                # Directory entries are never read, so they cost only pass time.
                self.assertEqual(result["unknown"], {})
                self.assertEqual(result["delivery_reads"], 0)
                self.assertLess(len(json.dumps(self.state()["delivery_observation"])), 4096)
                if result["removed"]:
                    break
            self.assertEqual(result["removed"], 3, result)
            # Each group once, plus the group names listed again by later passes.
            self.assertGreaterEqual(sum(counts), 32769 * 2)
            self.assertTrue(all(not home.exists() for home in homes))
            print(json.dumps({"g03_recipient_groups": 32769, "entries_by_pass": counts,
                              "durable_bytes": len(json.dumps(self.state()["delivery_observation"]))}))


    def test_g03_unrelated_publisher_in_large_existing_group_does_not_rescan_at_detach(self):
        attempt, recipient = "att-g03-large-group-free", "g03-large-existing-group"
        home = self.add(attempt, harness="codex")
        with VirtualDeliveries(self.root, self.now - 3 * RETENTION_NS,
                               recipient_keys=[recipient]) as history:
            at = self.now + 2 * RETENTION_NS
            first = R.sweep(self.jobs, now_ns=at)
            self.assertGreater(first["deferred"], 0)
            group = P.record_directory(self.root, recipient)
            original_check, original_scandir = R._late_candidate_delivery_change, os.scandir
            published, errors = [], []
            def check(ctx, selected, deadline, binding=None):
                if selected == attempt and attempt in AR.held_attempts() and not published:
                    def writer():
                        try:
                            self.publish_delivery("delivery-" + "f" * 32, recipient,
                                                  ["att-g03-large-group-unrelated"], "-")
                            published.append(True)
                        except Exception as exc:
                            errors.append(repr(exc))
                    child = threading.Thread(target=writer)
                    child.start(); child.join(5)
                    self.assertFalse(child.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(published, [True])
                return original_check(ctx, selected, deadline, binding)
            def scandir(path):
                actual = os.readlink(f"/proc/self/fd/{path}") if isinstance(path, int) else str(path)
                if attempt in AR.held_attempts():
                    self.assertNotEqual(actual, str(group), "history enumeration under final admission")
                return original_scandir(path)
            # The clock slack only tolerates the coarse kernel clock for groups touched
            # within a moment of the mark. This fixture's group is genuinely older than
            # the observation, which a few milliseconds of test setup cannot express.
            with mock.patch.object(R, "_late_candidate_delivery_change", check), \
                    mock.patch.object(R, "MARK_SLACK_NS", 0), self.replaced("scandir", scandir):
                result = R.sweep(self.jobs, now_ns=at + 301 * 10**9)
            self.assertEqual(result.get("removed"), 1, result)
            self.assertFalse(home.exists())
            self.assertEqual(published, [True])
            self.assertTrue(P.record_path(self.root, recipient, "delivery-" + "f" * 32).is_file())


    def _r1_assert(self, outcome, n_paths=1):
        self.assertEqual(outcome["exists"], [True] * n_paths, outcome)
        self.assertEqual(outcome["record_state"], "pending", outcome)
        self.assertEqual(outcome["second"].get("removed"), 0, outcome)
        self.assertEqual(outcome["second"].get("unknown", {}), {}, outcome)
        self.assertEqual(outcome["token_unchanged"], outcome["pinned"], outcome)

    def test_g03_r1_publication_timing_matrix(self):
        """R1: an old (pinned) or current writer publishing around a bounded observation never loses the HOME."""
        for timing in ("T1", "T2", "T3", "T4"):
            for pinned in (True, False):
                for relation in ("attempt", "parent"):
                    for new_group in (False, True):
                        with self.subTest(timing=timing, pinned=pinned, relation=relation, new_group=new_group):
                            case = Fixture(); case.setUp()
                            try:
                                attempt = "att-r1-matrix-home"
                                home = case.add(attempt, harness="codex")
                                outcome = r1_publication_run(
                                    case, attempt=attempt, paths=[home], sweep=R.sweep,
                                    at=case.now + 2 * RETENTION_NS, history_time=case.now - 3 * RETENTION_NS,
                                    timing=timing, pinned=pinned, relation=relation, new_group=new_group)
                                self._r1_assert(outcome)
                                print(json.dumps({"r1_home": outcome}))
                            finally:
                                case.doCleanups()

    def test_g03_r1_large_single_pass_keeps_publication_after_the_group_was_read(self):
        """A large (not resumed) single pass is protected by the same recheck, not by the root stamp alone."""
        for pinned in (True, False):
            for relation in ("attempt", "parent"):
                with self.subTest(pinned=pinned, relation=relation):
                    case = Fixture(); case.setUp()
                    try:
                        attempt = "att-r1-large-single-home"
                        home = case.add(attempt, harness="codex")
                        outcome = r1_publication_run(
                            case, attempt=attempt, paths=[home], sweep=R.sweep,
                            first_at=case.now, at=case.now + 2 * RETENTION_NS,
                            history_time=case.now, timing="L1", pinned=pinned, relation=relation,
                            new_group=False, real_history=120)
                        self.assertGreater(outcome["second"].get("delivery_reads", 0), 33, outcome)
                        self._r1_assert(outcome)
                        print(json.dumps({"r1_home_large_single": outcome}))
                    finally:
                        case.doCleanups()

    # --- R1 follow-ups: other groups, bindings, replacement, fs premise, progress ----

    def test_g03_r1_publication_into_a_group_the_candidate_does_not_name_is_protected(self):
        for timing in ("T1", "T4"):
            for relation in ("attempt", "parent"):
                with self.subTest(timing=timing, relation=relation):
                    case = Fixture(); case.setUp()
                    try:
                        attempt = "att-r1-other-group"
                        home = case.add(attempt, harness="codex")
                        outcome = r1_publication_run(
                            case, attempt=attempt, paths=[home], sweep=R.sweep,
                            at=case.now + 2 * RETENTION_NS, history_time=case.now - 3 * RETENTION_NS,
                            timing=timing, pinned=True, relation=relation, new_group=False, group_index=2)
                        self._r1_assert(outcome)
                    finally:
                        case.doCleanups()

    def test_g03_r1_home_key_binding_publication_is_protected(self):
        """An older writer naming an attempt that maps onto a legacy HOME key is fenced by the HOME view."""
        for timing in ("T1", "T2", "T4"):
            with self.subTest(timing=timing):
                case = Fixture(); case.setUp()
                try:
                    attempt = "att-r1-legacy-binding"
                    home = case.add_unmapped_legacy(attempt, harness="codex")
                    with case.process_inventory(os.getpid()):
                        case.sweep(0)
                        outcome = r1_publication_run(
                            case, attempt=attempt, paths=[home], sweep=R.sweep,
                            at=case.now + 2 * RETENTION_NS, history_time=case.now - 3 * RETENTION_NS,
                            timing=timing, pinned=True, relation="attempt", new_group=False)
                    self.assertEqual(outcome["exists"], [True], outcome)
                    self.assertEqual(outcome["record_state"], "pending", outcome)
                    self.assertEqual(outcome["second"].get("removed"), 0, outcome)
                    self.assertEqual(outcome["second"].get("unknown", {}), {}, outcome)
                finally:
                    case.doCleanups()

    def _r1_real_history_case(self, attempt, groups=3, per_group=40):
        case = Fixture(); case.setUp()
        home = case.add(attempt, harness="codex")
        paths = []
        for number in range(groups):
            group = P.record_directory(case.root, f"r1-real-{number}")
            group.mkdir(parents=True, mode=0o700, exist_ok=True)
            paths.append(group)
            for i in range(per_group):
                Fixture.delivery_record(case, group / f"delivery-{i:032x}.json", f"att-history-{number}-{i}")
        # Age every directory beyond the clock slack: an existing, already read history.
        old = time.time_ns() - 60 * 10**9
        for group in paths:
            os.utime(group, ns=(old, old))
        os.utime(case.root / "pending-delivery", ns=(old, old))
        case.sweep(0)
        return case, home, paths

    def test_g03_r1_group_directory_replaced_after_it_was_read_keeps_the_home(self):
        for variant in ("fresh-directory", "older-directory-moved-in"):
            with self.subTest(variant=variant):
                attempt = "att-r1-group-replaced"
                case, home, paths = self._r1_real_history_case(attempt)
                try:
                    target = paths[1]
                    real_late, done = R._late_candidate_delivery_change, []

                    def swap_then_check(ctx, selected, deadline, binding=None):
                        if not done:
                            aside = case.root / "aside-group"
                            replacement = case.root / "replacement-group"
                            replacement.mkdir(mode=0o700)
                            Fixture.delivery_record(case, replacement / ("delivery-" + "e" * 32 + ".json"),
                                                    attempt, state="pending")
                            if variant == "older-directory-moved-in":
                                old = time.time_ns() - 3600 * 10**9
                                os.utime(replacement, ns=(old, old))
                            os.rename(target, aside)
                            os.rename(replacement, target)
                            done.append(True)
                        return real_late(ctx, selected, deadline, binding)

                    with mock.patch.object(R, "_late_candidate_delivery_change", swap_then_check):
                        result = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
                    self.assertEqual(done, [True])
                    self.assertTrue(home.exists(), result)
                    self.assertEqual(result["removed"], 0, result)
                    self.assertEqual(result["unknown"], {}, result)
                finally:
                    case.doCleanups()

    def test_g03_r1_unrelated_changes_after_the_read_do_not_block_and_read_only_new_entries(self):
        attempt = "att-r1-unrelated-changes"
        case, home, paths = self._r1_real_history_case(attempt)
        try:
            real_late, real_relation, held_reads, done = (
                R._late_candidate_delivery_change, R._delivery_record_relation, [], [])

            def mutate_then_check(ctx, selected, deadline, binding=None):
                if not done:
                    done.append(True)
                    Fixture.delivery_record(case, paths[0] / ("delivery-" + "d" * 32 + ".json"), "att-unrelated-new")
                    Fixture.delivery_record(case, paths[1] / ("delivery-" + "c" * 32 + ".json"), "att-unrelated-newer",
                                            state="pending")
                    victim = sorted(paths[2].glob("*.json"))[0]
                    P._write_prune_tombstone  # the tombstone path is exercised in the prune tests
                    victim.unlink()
                return real_late(ctx, selected, deadline, binding)

            def counted_relation(path, related, binding=None, **kwargs):
                if attempt in AR.held_attempts():
                    held_reads.append(path.name)
                return real_relation(path, related, binding, **kwargs)

            with mock.patch.object(R, "_late_candidate_delivery_change", mutate_then_check), \
                    mock.patch.object(R, "_delivery_record_relation", counted_relation):
                result = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
            self.assertEqual(result["removed"], 1, result)
            self.assertFalse(home.exists())
            self.assertEqual(result["unknown"], {}, result)
            # Only the two entries published after the refresh may be read under admission... or none,
            # when the refresh already folded them in; never an old entry.
            self.assertLessEqual(len(held_reads), 2, held_reads)
            self.assertTrue(all(name in ("delivery-" + "d" * 32 + ".json", "delivery-" + "c" * 32 + ".json")
                                for name in held_reads), held_reads)
        finally:
            case.doCleanups()

    def test_g03_r1_rename_publication_updates_the_ctime_of_the_published_inode(self):
        """The premise of the mark rule, pinned on this filesystem (see the R1 plan)."""
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp) / "group"
            group.mkdir()
            old = time.time_ns() - 3600 * 10**9
            staged = group / ".staged"
            staged.write_text("{}")
            os.utime(staged, ns=(old, old))
            before_dir = group.stat().st_mtime_ns
            mark = time.time_ns()
            time.sleep(0.02)
            os.replace(staged, group / "delivery.json")
            published = (group / "delivery.json").stat()
            self.assertEqual(published.st_mtime_ns, old)
            self.assertGreaterEqual(published.st_ctime_ns, mark)
            self.assertGreaterEqual(group.stat().st_mtime_ns, mark)
            self.assertGreater(group.stat().st_mtime_ns, before_dir)

    def _r1_progress_case(self, attempt_suffix, *, unrelated_groups=2):
        case = Fixture(); case.setUp()
        free = case.add("att-r1-free-" + attempt_suffix, harness="codex")
        opened = case.add("att-r1-open-" + attempt_suffix, harness="codex")
        parent = case.add("att-r1-parent-" + attempt_suffix, harness="codex")
        keys = [f"r1-{attempt_suffix}-{i}" for i in range(3)]
        un = [f"r1-unrelated-{attempt_suffix}-{i}" for i in range(unrelated_groups)]
        return case, free, opened, parent, keys, un

    def test_g03_r1_unrelated_publications_never_stretch_the_observation(self):
        case, free, opened, parent, keys, un = self._r1_progress_case("p1")
        try:
            for index, key in enumerate(un):
                case.publish_delivery("delivery-" + f"{index + 1:032x}", key, [f"att-unrelated-seed-{index}"], "-")
            with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS, groups=3, recipient_keys=keys,
                                   open_refs=[(["att-r1-open-p1"], "att-r1-parent-p1")]) as history:
                serial, passes, held_scans = [100], [], []
                real_page, real_scan = R._directory_page, R._admission_scan

                def publish(n=1):
                    for _ in range(n):
                        serial[0] += 1
                        # existing groups only; a new group would legitimately restart the traversal
                        case.publish_delivery("delivery-" + f"{serial[0]:032x}", un[serial[0] % len(un)],
                                              [f"att-unrelated-{serial[0]}"], "-")

                def page(path, cookie, limit, deadline):
                    entries, eof, stamp = real_page(path, cookie, limit, deadline)
                    if Path(path) in history.paths and eof:
                        publish()
                    return entries, eof, stamp

                def scan(ctx, deadline):
                    # A normal writer in another thread, as in the admission-time races above.
                    writer = threading.Thread(target=publish)
                    writer.start(); writer.join(5)
                    self.assertFalse(writer.is_alive())
                    outcome = real_scan(ctx, deadline)
                    held_scans.append(outcome)
                    return outcome

                at = case.now + 2 * RETENTION_NS
                with mock.patch.object(R, "_directory_page", page), mock.patch.object(R, "_admission_scan", scan):
                    for i in range(6):
                        result = R.sweep(case.jobs, now_ns=at + i * 301 * 10**9)
                        passes.append(result)
                        self.assertNotIn("skipped", result)
                        self.assertLessEqual(result["delivery_reads"], R.DELIVERY_READ_BUDGET, result)
                        self.assertEqual(result["unknown"], {}, result)
                        self.assertTrue(opened.exists() and parent.exists())
                        if not free.exists():
                            break
                        publish(5)
                self.assertFalse(free.exists(), passes)
                self.assertLessEqual(len(passes), 3, passes)
                self.assertTrue(all(outcome is None for outcome in held_scans), held_scans)
                print(json.dumps({"g03_r1_progress_passes": len(passes), "scans": held_scans}))
        finally:
            case.doCleanups()

    def test_g03_r1_admission_reread_budget_defers_without_deleting_and_later_passes_remove(self):
        attempt = "att-r1-reread-budget"
        case, home, paths = self._r1_real_history_case(attempt)
        try:
            real_scan, stage = R._admission_scan, {"n": 0}

            def burst(count, tag):
                for i in range(count):
                    Fixture.delivery_record(case, paths[0] / (f"delivery-{tag}{i:031x}.json"), f"att-unrelated-{tag}{i}")

            def scan(ctx, deadline):
                if stage["n"] == 0:
                    stage["n"] = 1
                    burst(R.ADMISSION_REREAD_LIMIT + 8, "a")
                return real_scan(ctx, deadline)

            with mock.patch.object(R, "_admission_scan", scan):
                first = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
            self.assertEqual(first["removed"], 0, first)
            self.assertEqual(self.kept(first, "defer:delivery-reread-budget"), 1, first)
            self.assertEqual(first["unknown"], {}, first)
            self.assertTrue(home.exists())
            second = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS + 301 * 10**9)
            self.assertEqual(second["removed"], 1, second)
            self.assertFalse(home.exists())
        finally:
            case.doCleanups()

    def test_g03_r1_publication_after_the_admission_look_is_not_read_again(self):
        attempt = "att-r1-after-look"
        case, home, paths = self._r1_real_history_case(attempt)
        try:
            real_late, real_scan, scandirs, done = (R._late_candidate_delivery_change, R._admission_scan, [], [])
            real_scandir = os.scandir

            def check(ctx, selected, deadline, binding=None):
                if attempt in AR.held_attempts() and not done:
                    done.append(True)
                    for i in range(R.ADMISSION_REREAD_LIMIT + 8):
                        Fixture.delivery_record(case, paths[0] / (f"delivery-b{i:031x}.json"), f"att-unrelated-b{i}")
                return real_late(ctx, selected, deadline, binding)

            def scandir(path):
                if attempt in AR.held_attempts() and done:
                    scandirs.append(path)
                return real_scandir(path)

            with mock.patch.object(R, "_late_candidate_delivery_change", check), \
                    self.replaced("scandir", scandir):
                result = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
            self.assertEqual(done, [True])
            self.assertEqual(result["removed"], 1, result)
            self.assertEqual(scandirs, [])
        finally:
            case.doCleanups()

    def test_g03_r1_pinned_unrelated_writer_between_passes_is_safe_and_progress_resumes_when_it_stops(self):
        case, free, opened, parent, keys, un = self._r1_progress_case("p4", unrelated_groups=1)
        try:
            with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS, groups=3, recipient_keys=keys,
                                   open_refs=[(["att-r1-open-p4"], "att-r1-parent-p4")]):
                case.publish_delivery("delivery-" + "1" * 32, un[0], ["att-unrelated-seed"], "-")
                at = case.now + 2 * RETENTION_NS
                for i in range(3):
                    with normal_delivery_writer(case.root, un[0], "att-unrelated-pinned", pinned=True):
                        pass
                    result = R.sweep(case.jobs, now_ns=at + i * 301 * 10**9)
                    self.assertEqual(result["unknown"], {}, result)
                    self.assertTrue(opened.exists() and parent.exists(), result)
                # The old writer re-modes the store on every publication. That moves
                # its ctime but replaces nothing, so the read is not restarted and the
                # unrelated HOME is retired while the writer is still active.
                self.assertFalse(free.exists(), result)
        finally:
            case.doCleanups()

    def test_g03_r1_thousands_of_real_recipient_groups_never_defer_globally(self):
        """Group count is not a global limit: the final look stats each group once, no cap."""
        attempt = "att-r1-many-real-groups"
        case = Fixture(); case.setUp()
        try:
            home = case.add(attempt, harness="codex")
            base = case.root / "pending-delivery"
            base.mkdir(mode=0o700)
            count = 4600
            for number in range(count):
                (base / f"{number + 1:064x}").mkdir(mode=0o700)
            old = time.time_ns() - 60 * 10**9
            for group in base.iterdir():
                os.utime(group, ns=(old, old))
            os.utime(base, ns=(old, old))
            at = case.now + 2 * RETENTION_NS
            seen = []
            for i in range(8):
                result = R.sweep(case.jobs, now_ns=at + i * 301 * 10**9)
                seen.append(result)
                self.assertEqual(result["unknown"], {}, result)
                for reason in result["kept"]:
                    self.assertNotIn("budget", reason, result)
                    self.assertNotIn("groups-over", reason, result)
                if result["removed"]:
                    break
            self.assertFalse(home.exists(), seen)
            # Group entries are never read, so they cost only pass time, never the read budget
            self.assertLessEqual(len(seen), 4, seen)
        finally:
            case.doCleanups()

    def test_g03_r1_record_for_another_cohort_member_read_by_a_refresh_still_protects_that_member(self):
        """A refresh moves the shared mark; the member it did not belong to must still see the record."""
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                case = Fixture(); case.setUp()
                try:
                    homes = {a: case.add(a, harness="codex") for a in ("att-r1-member-a", "att-r1-member-b")}
                    for number in range(2):
                        group = P.record_directory(case.root, f"r1-member-{number}")
                        group.mkdir(parents=True, mode=0o700, exist_ok=True)
                        for i in range(40):
                            Fixture.delivery_record(case, group / f"delivery-{i:032x}.json", f"att-history-{number}-{i}")
                    old = time.time_ns() - 60 * 10**9
                    for path in (case.root / "pending-delivery").rglob("*"):
                        os.utime(path, ns=(old, old))
                    os.utime(case.root / "pending-delivery", ns=(old, old))
                    case.sweep(0)
                    real_late, state = R._late_candidate_delivery_change, {}

                    def check(ctx, selected, deadline, binding=None):
                        if "first" not in state:
                            state["first"] = selected
                            state["other"] = next(a for a in homes if a != selected)
                            # published after both verdicts; it names only the member not being detached
                            with normal_delivery_writer(case.root, "r1-member-0", state["other"], pinned=pinned):
                                pass
                        return real_late(ctx, selected, deadline, binding)

                    with mock.patch.object(R, "_late_candidate_delivery_change", check):
                        result = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
                    self.assertEqual(result["removed"], 1, result)
                    self.assertFalse(homes[state["first"]].exists())
                    self.assertTrue(homes[state["other"]].exists(), result)
                    self.assertEqual(P.read(case.root, "r1-member-0", "delivery-" + "f" * 32)["state"], "pending")
                finally:
                    case.doCleanups()

    def test_g03_r1_refresh_budget_overrun_defers_without_deleting(self):
        attempt = "att-r1-refresh-budget"
        case, home, paths = self._r1_real_history_case(attempt)
        try:
            real_late, done = R._late_candidate_delivery_change, []

            def check(ctx, selected, deadline, binding=None):
                if not done:
                    done.append(True)
                    # more entries published since the mark than one refresh may read
                    for n in range(8):
                        Fixture.delivery_record(case, paths[0] / f"delivery-{0xd000 + n:032x}.json",
                                                f"att-unrelated-new-{n}")
                    patched = mock.patch.object(R, "DELIVERY_READ_BUDGET", 5)
                    patched.start()
                    self.addCleanup(patched.stop)
                return real_late(ctx, selected, deadline, binding)

            with mock.patch.object(R, "_late_candidate_delivery_change", check):
                first = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
            self.assertEqual(first["removed"], 0, first)
            self.assertEqual(self.kept(first, "defer:delivery-refresh-budget"), 1, first)
            self.assertEqual(first["unknown"], {}, first)
            self.assertTrue(home.exists())
            mock.patch.stopall()
            second = third = None
            for i in range(1, 5):
                second = R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS + i * 301 * 10**9)
                if second["removed"]:
                    break
            self.assertEqual(second["removed"], 1, second)
            self.assertFalse(home.exists())
        finally:
            case.doCleanups()

    def _r1_progress_under_churn_case(self, suffix, *, groups=3, per_group=20):
        """Aged unrelated groups, one open reference, a free candidate and a related parent."""
        case = Fixture(); case.setUp()
        free = case.add("att-r1-churn-free-" + suffix, harness="codex")
        opened = case.add("att-r1-churn-open-" + suffix, harness="codex")
        parent = case.add("att-r1-churn-parent-" + suffix, harness="codex")
        paths = []
        for number in range(groups):
            group = P.record_directory(case.root, f"r1-churn-{suffix}-{number}")
            paths.append(group)
            for i in range(per_group):
                Fixture.delivery_record(case, group / f"delivery-{i:032x}.json", f"att-churn-{number}-{i}")
        held = P.record_directory(case.root, f"r1-churn-{suffix}-open")
        Fixture.delivery_record(case, held / ("delivery-" + "f" * 32 + ".json"), "att-r1-churn-open-" + suffix,
                                parent="att-r1-churn-parent-" + suffix, state="pending")
        return case, free, opened, parent, paths

    def _r1_churn_between_passes(self, case, paths, round_number):
        """What a busy system does between two passes: publications, a prune-like unlink, a touch."""
        for number, group in enumerate(paths):
            Fixture.delivery_record(case, group / f"delivery-{0xa000 + round_number * 16 + number:032x}.json",
                                    f"att-churn-new-{round_number}-{number}")
            victims = sorted(group.glob("delivery-*.json"))
            if victims:
                victims[0].unlink()
        os.utime(paths[-1])

    def test_g03_r1_refresh_budget_counts_entries_read_not_entries_in_changed_groups(self):
        """N > cap while every pass changes several groups whose entries sum past the cap.

        The saved mark only moves when a refresh completes, so a refresh that charged
        every `.json` of a changed group (read or not) would defer on each observation.
        """
        case, free, opened, parent, paths = self._r1_progress_under_churn_case("a")
        try:
            time.sleep(0.05)  # fixture records are older than the (patched) slack
            total = sum(1 for _ in (case.root / "pending-delivery").glob("*/*.json"))
            passes, deferred = [], []
            at = case.now + 2 * RETENTION_NS
            with mock.patch.object(R, "DELIVERY_READ_BUDGET", 40), \
                    mock.patch.object(R, "MARK_SLACK_NS", 5 * 10**6):
                self.assertGreater(total, R.DELIVERY_READ_BUDGET)
                for i in range(10):
                    result = R.sweep(case.jobs, now_ns=at + i * 301 * 10**9)
                    passes.append(result)
                    deferred.append(self.kept(result, "defer:delivery-refresh-budget"))
                    self.assertNotIn("skipped", result)
                    self.assertEqual(result["unknown"], {}, result)
                    self.assertTrue(opened.exists() and parent.exists(), result)
                    if not free.exists():
                        break
                    self._r1_churn_between_passes(case, paths, i)
            print(json.dumps({"g03_r1_churn_passes": len(passes), "refresh_budget_deferrals": deferred,
                              "records": total}))
            self.assertFalse(free.exists(), (len(passes), deferred, passes[-1]))
            self.assertLessEqual(len(passes), 8, (len(passes), deferred))
            self.assertEqual(sum(deferred), 0, deferred)
        finally:
            case.doCleanups()

    def test_g03_r1_records_inside_the_clock_slack_keep_every_pass_bounded_and_still_finish(self):
        """The slack may make a refresh re-read just-made entries; a pass stays bounded and the removal still comes."""
        case, free, opened, parent, paths = self._r1_progress_under_churn_case("s")
        try:
            slack = 10**8
            passes, reasons, at = [], [], case.now + 2 * RETENTION_NS
            with mock.patch.object(R, "DELIVERY_READ_BUDGET", 40), mock.patch.object(R, "MARK_SLACK_NS", slack):
                for i in range(12):
                    # the fixture was made inside the first observation's slack; passes are minutes apart
                    if i:
                        time.sleep(slack / 1e9 + 0.05)
                    result = R.sweep(case.jobs, now_ns=at + i * 301 * 10**9)
                    passes.append(result)
                    reasons.append(sorted(result["kept"]))
                    self.assertEqual(result["unknown"], {}, result)
                    self.assertTrue(opened.exists() and parent.exists(), result)
                    # one pass reads at most the entries it visits, a refresh's reads and the admission's few
                    self.assertLessEqual(result["delivery_reads"],
                                         2 * R.DELIVERY_READ_BUDGET + R.ADMISSION_REREAD_LIMIT, result)
                    if not free.exists():
                        break
                    self._r1_churn_between_passes(case, paths, i)
            print(json.dumps({"g03_r1_slack_passes": len(passes), "kept": reasons,
                              "reads": [r["delivery_reads"] for r in passes]}))
            self.assertFalse(free.exists(), (len(passes), reasons))
            self.assertLessEqual(len(passes), 8, reasons)
        finally:
            case.doCleanups()

    def test_g03_r1_saved_progress_holds_one_mark_and_no_per_group_or_per_record_state(self):
        case, free, opened, parent, keys, un = self._r1_progress_case("s1")
        try:
            with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS, groups=3, recipient_keys=keys,
                                   open_refs=[(["att-r1-open-s1"], "att-r1-parent-s1")]):
                R.sweep(case.jobs, now_ns=case.now + 2 * RETENTION_NS)
                saved = case.state()["delivery_observation"]
                self.assertIsInstance(saved["mark"], int)
                self.assertNotIn("groups", saved)
                self.assertNotRegex(json.dumps(saved), r"delivery-[0-9a-f]{32}")
                self.assertLess(len(json.dumps(saved)), 4096)
        finally:
            case.doCleanups()

    def test_writer_lines_written_together_are_both_seen(self):
        """The fixture reads a writer's progress lines even when one pipe read
        returns both; a buffered readline() used to hide the second line and the
        wait then failed after ten seconds (CI run 41571381252)."""
        code = ("import sys; print('reference-attempt'); print('record-admitted', flush=True); "
                "sys.stdin.readline()")
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        try:
            started = time.monotonic()
            wait_for_record_admitted(child)
            self.assertLess(time.monotonic() - started, 5)
        finally:
            child.stdin.write("\n"); child.stdin.flush()
            child.communicate(timeout=10)

    # --- RV-1079 review fixes ---------------------------------------------------
    def test_rv1079_related_change_or_new_group_does_not_restart_the_cohort(self):
        """M1: a cohort that spans passes keeps its progress when one member's related
        attempts change (a child ends) or a recipient group is added. That member alone
        waits for a fresh token; the others are judged in the next pass."""
        for change in ("child", "new-group"):
            with self.subTest(change=change):
                case = Fixture(); case.setUp()
                try:
                    free = case.add("att-m1-free", harness="codex")
                    owner = case.add("att-m1-owner", "running", harness="codex")
                    with VirtualDeliveries(case.root, case.now - 3 * RETENTION_NS, groups=3):
                        at = case.now + 2 * RETENTION_NS
                        first = R.sweep(case.jobs, now_ns=at)
                        self.assertGreater(first["deferred"], 0, first)
                        identity = case.state()["delivery_observation"]["identity"]
                        if change == "child":
                            case.row("att-m1-child", "done", parent_attempt_id="att-m1-owner",
                                     delivery_intent="1")
                        else:
                            case.publish_delivery("delivery-" + "e" * 32, "rv1079-new-group",
                                                  ["att-m1-other"], "-")
                        second = R.sweep(case.jobs, now_ns=at + 301 * 10**9)
                        self.assertEqual(case.state()["delivery_observation"]["identity"], identity)
                        self.assertLess(second["delivery_reads"], 32, second)
                        self.assertEqual(second["unknown"], {}, second)
                        self.assertEqual(second["removed"], 1, second)
                        self.assertFalse(free.exists())
                        self.assertTrue(owner.exists())
                finally:
                    case.doCleanups()

    def test_rv1079_a_token_that_cannot_be_written_defers_only_its_home(self):
        """M2: a full disk while a HOME's reference token is first written keeps that
        HOME (deferred); the pass still runs, judges and removes the others."""
        import errno
        early = self.add("att-m2-early", harness="codex")
        self.sweep(0)  # writes the early HOME's token and starts its clock
        late = self.add("att-m2-late", harness="codex")
        real = P._change_token

        def full(path, *, advance=False):
            if advance:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, advance=advance)

        with mock.patch.object(P, "_change_token", full):
            result = self.sweep(8 / 7)
        self.assertNotIn("skipped", result)
        self.assertEqual(result["removed"], 1, result)
        self.assertEqual(self.kept(result, "delivery-read-deferred"), 1, result)
        self.assertFalse(early.exists())
        self.assertTrue(late.exists())
        self.assertEqual(self.sweep(9 / 7)["unknown"], {})

    def test_rv1079_without_getdents64_a_sorted_listing_resumes_and_judges(self):
        """M3: where libc has no getdents64 (macOS, glibc < 2.30) the observation
        still resumes across passes and decides, instead of deferring forever."""
        free = self.add("att-m3-free", harness="codex")
        opened = self.add("att-m3-open", harness="codex")
        for index in range(7):
            self.publish_delivery(f"delivery-{index + 1:032x}", f"rv1079-m3-{index % 2}",
                                  [f"att-m3-history-{index}"], "-")
        self.publish_delivery("delivery-" + "e" * 32, "rv1079-m3-1", ["att-m3-open"], "-")
        time.sleep(0.05)  # the records are older than the (patched) clock slack
        at = self.now + 2 * RETENTION_NS
        with mock.patch.object(R, "_getdents64", lambda: None), \
                mock.patch.object(R, "DELIVERY_READ_BUDGET", 3), \
                mock.patch.object(R, "MARK_SLACK_NS", 5 * 10**6):
            self.assertEqual(R._listing_mode(), "sorted")
            passes = []
            for i in range(8):
                result = R.sweep(self.jobs, now_ns=at + i * 301 * 10**9)
                passes.append(result)
                self.assertEqual(result["unknown"], {}, result)
                self.assertLessEqual(result["delivery_reads"], 3, result)
                if not free.exists():
                    break
        self.assertGreater(passes[0]["deferred"], 0, passes)
        self.assertFalse(free.exists(), passes)
        self.assertTrue(opened.exists())
        self.assertEqual(self.kept(passes[-1], "delivery-open"), 1, passes[-1])

    def test_rv1079_a_rejected_late_record_stays_new_for_the_next_candidate(self):
        """S1: a record published after the observation mark that cannot be read keeps
        every candidate whose late check meets it, not only the first one."""
        homes = [self.add(f"att-s1-{name}", harness="codex") for name in ("a", "b")]
        for index in range(40):  # more than 32 files: the mark-based late check runs
            self.publish_delivery(f"delivery-{index + 1:032x}", "rv1079-s1",
                                  [f"att-s1-history-{index}"], "-")
        group = P.record_directory(self.root, "rv1079-s1")
        real, written = R._late_candidate_delivery_change, []

        def late(ctx, attempt, deadline, binding=None):
            if not written:
                # An old or foreign writer: no token moves, the body cannot be read.
                (group / ("delivery-" + "d" * 32 + ".json")).write_text("{")
                written.append(True)
            return real(ctx, attempt, deadline, binding)

        with mock.patch.object(R, "_late_candidate_delivery_change", late):
            result = R.sweep(self.jobs, now_ns=self.now + 2 * RETENTION_NS)
        self.assertEqual(written, [True])
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(self.kept(result, "late-evidence-unreadable"), 2, result)
        self.assertTrue(all(home.exists() for home in homes))

    def test_rv1079_a_ledger_anomaly_keeps_only_identity_less_homes(self):
        """S2: an unexpected ledger file keeps the identity-less HOMEs that need the
        ledger; HOMEs with an exact identity in the same cohort are still judged."""
        free = self.add("att-s2-free")
        legacy = self.add_unmapped_legacy("att-s2-legacy")
        ledger = self.root / "supervisor-state"
        ledger.mkdir(exist_ok=True)
        (ledger / "not-an-attempt.json").write_text("{}")
        result = self.expire()
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(free.exists())
        self.assertTrue(legacy.exists())
        self.assertNotIn("delivery-index-unknown", result["unknown"], result)


class BusyRegistry(Fixture):
    """Owners keep rewriting registry rows while a pass runs (2026-11-10 backlog).

    Registry writers replace the file atomically with a rewritten tail
    (`dispatch_contract_registry._atomic_registry_replace`). A change that does
    not concern the candidate must not keep its home, however often it lands.
    """

    def replace_rows(self):
        import dispatch_contract_registry as DCR
        lines = []
        for attempt, (status, meta) in self.rows.items():
            pipe = ",".join(f"{k}={v}" for k, v in meta.items())
            timestamp = self.timestamps.get(attempt) or datetime.fromtimestamp(
                self.now / 10**9, timezone.utc).isoformat().replace("+00:00", "Z")
            lines.append(f"{timestamp}\t{status}\t/repo\t/wt\t{attempt}\t{pipe}")
        DCR._atomic_registry_replace(self.jobs, lines)

    @contextlib.contextmanager
    def every_lock(self, mutate):
        """Run `mutate` before every registry lock a detach takes."""
        original = R._registry_lock
        fired = []

        @contextlib.contextmanager
        def lock(jobs):
            fired.append(True)
            mutate()
            with original(jobs) as admitted:
                yield admitted

        with mock.patch.object(R, "_registry_lock", lock):
            yield fired

    def tick(self, attempt):
        def mutate():
            status, meta = self.rows[attempt]
            meta = {**meta, "noise_tick": str(int(meta.get("noise_tick", "0")) + 1)}
            self.rows[attempt] = (status, meta)
            self.replace_rows()
        return mutate

    def test_repeated_unrelated_tail_rewrites_do_not_keep_the_home(self):
        home = self.add("att-busy-candidate")
        self.row("att-busy-noise", "running")
        self.sweep(0)
        with self.every_lock(self.tick("att-busy-noise")) as fired:
            result = self.sweep(8 / 7)
        self.assertGreaterEqual(len(fired), 2, result)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())

    def test_rewrite_of_the_candidate_row_keeps_the_home(self):
        home = self.add("att-busy-rewritten")
        self.row("att-busy-other", "running")
        self.sweep(0)
        with self.every_lock(self.tick("att-busy-rewritten")):
            result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 0, result)
        self.assertTrue(home.exists())

    def test_rewrite_without_lineage_still_defers_instead_of_guessing(self):
        home = self.add("att-busy-unproven")
        self.row("att-busy-noise", "running")
        self.sweep(0)

        def mutate():
            self.tick("att-busy-noise")()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(str(self.jobs) + ".lineage.json")

        with self.every_lock(mutate):
            result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 0, result)
        self.assertTrue(home.exists())
        self.sweep(16 / 7)
        self.assertFalse(home.exists())

    def test_late_row_naming_the_candidate_keeps_the_home(self):
        home = self.add("att-busy-parent")
        self.sweep(0)
        with self.landing(lambda: self.append_row(
                "att-busy-child", "done", parent_attempt_id="att-busy-parent")):
            result = self.sweep(8 / 7)
        self.assertEqual(result["removed"], 0, result)
        self.assertTrue(home.exists())
        # The next pass judges the row it now sees; the deferral is not a keep.
        self.sweep(16 / 7)
        self.assertFalse(home.exists())

    def test_homes_removed_by_hand_in_eligible_since_neither_stop_nor_fail_a_pass(self):
        home = self.add("att-busy-survivor")
        self.sweep(0)
        state = self.state()
        gone = {("%032x" % n): self.now for n in range(1, 1500)}
        state["eligible_since"].update(gone)
        (self.root / "homes/worker-retention/state.json").write_text(json.dumps(state))
        result = self.sweep(8 / 7)
        self.assertNotIn("skipped", result)
        self.assertEqual(result["removed"], 1, result)
        self.assertFalse(home.exists())
        self.assertEqual(self.state()["eligible_since"], {})

    def test_route_observer_fences_the_registry_by_rows_not_by_file_stamp(self):
        watched = {}
        token = R._DEPENDENCIES.set(watched)
        try:
            R._route_observer("file", self.jobs.resolve(), self.jobs.resolve())
        finally:
            R._DEPENDENCIES.reset(token)
        self.assertNotIn(self.jobs.resolve(), watched)
        self.replace_rows()
        self.assertTrue(R._fresh(watched))


class SharedCodexHomes(Fixture):
    """Nested Codex owner homes follow the worker-home rule for every claimant."""

    def codex_home(self, name="c" * 32 + "." + "d" * 12, identity="att-codex-owner"):
        path = self.root / "homes" / "codex" / name
        path.mkdir(parents=True)
        manifest = {"harness": "codex", "worker_type": "owner", "source": SOURCE,
                    "profile": None, "schema": 2, "key": "e" * 32, "destination": True}
        if identity:
            manifest["identity"] = identity
        (path / "worker-home.json").write_text(json.dumps(manifest))
        (path / "sessions").mkdir()
        (path / "sessions" / "rollout.jsonl").write_text("x" * 100)
        return path

    def claim(self, attempt, path, status="done"):
        self.row(attempt, status, harness="codex", worker_type="owner",
                 runtime_home=str(path))

    def test_home_whose_claimants_all_ended_is_retired(self):
        home = self.codex_home()
        self.claim("att-codex-owner", home)
        self.claim("att-codex-second", home)
        result = self.expire()
        self.assertEqual(result["codex"]["removed"], 1, result)
        self.assertFalse(home.exists())
        self.assertEqual(list((self.root / "homes/worker-retention/retiring").iterdir()), [])

    def test_one_running_claimant_keeps_the_shared_home(self):
        home = self.codex_home()
        self.claim("att-codex-owner", home)
        self.claim("att-codex-running", home, status="running")
        result = self.expire((30) / 7)
        self.assertEqual(result["codex"]["removed"], 0, result)
        self.assertTrue(home.is_dir())

    def test_unclaimed_or_foreign_shaped_homes_are_kept(self):
        unclaimed = self.codex_home(identity=None)
        legacy = self.root / "homes" / "codex" / ("a" * 32)
        legacy.mkdir()
        old = (self.now - int(100 / 7 * RETENTION_NS)) / 1e9
        for path in (unclaimed, legacy):
            os.utime(path, (old, old))
        result = self.expire((30) / 7)
        self.assertEqual(result["codex"]["unknown"].get("codex-unclaimed"), 1, result)
        self.assertTrue(unclaimed.is_dir() and legacy.is_dir())

    def test_recent_session_write_and_fleet_view_keep_the_home(self):
        home = self.codex_home()
        self.claim("att-codex-owner", home)
        self.sweep(0)
        later = (self.now + int(8 / 7 * RETENTION_NS)) / 1e9
        os.utime(home / "sessions", (later, later))
        result = self.sweep(8 / 7)
        self.assertEqual(result["codex"]["removed"], 0, result)
        viewed = (self.now + int(15 / 7 * RETENTION_NS)) / 1e9
        self.assertTrue(R.note_view(self.jobs, str(home), now=viewed))
        self.assertTrue((self.root / "homes/worker-retention/views" / home.name).is_file())
        result = self.sweep(16 / 7)
        self.assertEqual(result["codex"]["removed"], 0, result)
        self.assertTrue(home.is_dir())
        result = self.sweep(24 / 7)
        self.assertEqual(result["codex"]["removed"], 1, result)
        self.assertFalse((self.root / "homes/worker-retention/views" / home.name).exists())


if __name__ == "__main__":
    unittest.main()
