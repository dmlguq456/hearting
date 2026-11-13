#!/usr/bin/env python3
"""Cross-harness parity contract (plan.md §7, D-1).

Surface ① (session registry, `tools/fleet/session_registry.py`, §7.1/§7.4) and
surface ② (peer-message ledger record format, `utilities/peer-message.py` +
`tools/fleet/collectors/peer_messages.py`, §7.3) are parameterized across every
harness Fleet knows about (`HARNESSES`); an unimplemented writer or registry is
asserted directly (e.g. ``writer_support("opencode") == "not-implemented"``),
never skipped.

Surface ③ (refresh-pump recovery, `tools/fleet/refresh.py`) is NOT parameterized
by harness -- see REFRESH_PUMP_IS_HARNESS_NEUTRAL below.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from unittest import mock                                          # noqa: E402

from fleet import model, refresh, route_chain, session_registry   # noqa: E402
from fleet.collectors import liveness                             # noqa: E402
from fleet.collectors import peer_messages                        # noqa: E402
from fleet.model import Session                                   # noqa: E402

HARNESSES = ("claude", "codex", "opencode")

# 표면 ③은 하네스 중립이다. RefreshPump(tools/fleet/refresh.py:64)는 producer를
# 호출하는 primitive이고, 하네스별로 다른 실패 모드를 낸다는 observable contract가
# 없다. 그래서 표면 ③에는 하네스 파라미터를 붙이지 않는다 — 표면 ①②만 3형제로
# 파라미터화한다. 없는 계약을 만들지 않는 것이 이 파일의 규칙이다.
# test_f71_refresh.py는 복구 '행동'을, 여기는 health() 표면의 '모양'을 고정한다.
REFRESH_PUMP_IS_HARNESS_NEUTRAL = True


def _load_hyphenated(name, relative_path):
    """Import a hyphenated-filename module (can't `import` it normally)."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Surface ① — session registry (§7.1/§7.4)
# --------------------------------------------------------------------------

class _SessionRegistryDirMixin:
    """Isolates FLEET_SESSION_REGISTRY_DIR (codex/opencode) and a private claude
    "home" (via the `home=` kwarg session_registry.py exposes) so this test never
    touches this machine's real ~/.claude/sessions or ~/.local/state registry."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_registry_dir = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = os.path.join(self._tmp.name, "state")
        self.addCleanup(self._restore_registry_dir)
        self.claude_home = os.path.join(self._tmp.name, "claude-home")

    def _restore_registry_dir(self):
        if self._old_registry_dir is None:
            os.environ.pop("FLEET_SESSION_REGISTRY_DIR", None)
        else:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._old_registry_dir

    def _home_for(self, harness):
        # Only the claude branch of session_registry._dir_for() honors `home=`;
        # codex/opencode always resolve through state_root() (FLEET_SESSION_REGISTRY_DIR).
        return self.claude_home if harness == "claude" else None

    def _write_raw(self, harness, pid, payload):
        """Places a record without going through write() -- claude/opencode have no
        hearting-managed writer (WRITER_SUPPORT), so this is the only way to put a
        file where read() will find it for those two harnesses."""
        path = session_registry.registry_path(harness, pid, home=self._home_for(harness))
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)


class WriterSupportTest(unittest.TestCase):
    def test_writer_support_matches_the_declared_map(self):
        for h in HARNESSES:
            with self.subTest(harness=h):
                self.assertEqual(
                    session_registry.writer_support(h), session_registry.WRITER_SUPPORT[h])
        # Explicit, positively-checked values per harness -- an unimplemented
        # writer is asserted, never skipped (plan.md D-1).
        self.assertEqual(session_registry.writer_support("claude"), "runtime-native")
        self.assertEqual(session_registry.writer_support("codex"), "hearting-managed")
        self.assertEqual(session_registry.writer_support("opencode"), "not-implemented")


class FieldSetAndAbsentRecordTest(_SessionRegistryDirMixin, unittest.TestCase):
    def test_every_field_key_is_present_even_for_a_partial_record(self):
        for i, h in enumerate(HARNESSES):
            with self.subTest(harness=h):
                pid = 20000 + i
                self._write_raw(h, pid, {"pid": pid, "cwd": "/repo"})
                record = session_registry.read(h, pid, home=self._home_for(h))
                self.assertEqual(set(record), set(session_registry.FIELDS))
                self.assertEqual(record["cwd"], "/repo")
                self.assertIsNone(record["status"])
                self.assertIsNone(record["sessionId"])
                self.assertEqual(record["harness"], h)

    def test_absent_file_is_none_and_nothing_gets_applied(self):
        for i, h in enumerate(HARNESSES):
            with self.subTest(harness=h):
                pid = 30000 + i
                self.assertIsNone(session_registry.read(h, pid, home=self._home_for(h)))
                # No real collector calls apply_to_session on a None record -- a
                # freshly constructed Session already models that absence.
                sess = Session(harness=h, pid=pid)
                self.assertIsNone(sess.status)


class ProcStartMismatchTest(unittest.TestCase):
    def setUp(self):
        model.reset_state_tracker()

    def test_proc_start_mismatch_is_dead_for_every_harness(self):
        for h in HARNESSES:
            with self.subTest(harness=h):
                sess = Session(harness=h, pid=os.getpid(), cwd="/repo")
                record = {field: None for field in session_registry.FIELDS}
                record.update(status="busy", procStart="not-the-real-one")
                session_registry.apply_to_session(sess, record, h)
                state = liveness.classify(sess, time.time())
                self.assertEqual(state, "dead")


class WriterGateTest(_SessionRegistryDirMixin, unittest.TestCase):
    def test_hearting_managed_round_trips_others_refuse(self):
        for i, h in enumerate(HARNESSES):
            with self.subTest(harness=h):
                pid = 40000 + i
                if session_registry.writer_support(h) == "hearting-managed":
                    session_registry.write(h, pid, {"cwd": "/repo"})
                    self.assertEqual(session_registry.read(h, pid)["cwd"], "/repo")
                    self.assertTrue(session_registry.remove(h, pid))
                else:
                    with self.assertRaises(session_registry.RegistryWriteUnsupported):
                        session_registry.write(h, pid, {"cwd": "/repo"})
                    with self.assertRaises(session_registry.RegistryWriteUnsupported):
                        session_registry.remove(h, pid)


# --------------------------------------------------------------------------
# Surface ② — peer-message ledger record format (§7.3)
# --------------------------------------------------------------------------

class _PeerLedgerDirMixin:
    """Full isolation for the peer-message ledger (writer + fleet-reader chain).

    Mirrors `utilities/peer_message.test.py`'s `_TmpRootMixin`: AGENT_PEER_LEDGER_ROOT
    alone is not enough once a record is *read* back through `steward_marker_roots()`'s
    "fleet-reader" branch, which also probes `dispatch_state_roots()` and each
    installed runtime's own root (~/.codex, ~/.claude, ~/.config/opencode) -- all
    keyed off HOME. Overriding HOME closes all three (CODEX_HOME/CLAUDE_CONFIG_DIR
    are unset in the real environment too, so popping them is enough).
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        jobs_path = self.tmp_root / "jobs.log"
        jobs_path.touch()
        self._old_environ = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(jobs_path)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root / "peer-state")
        os.environ["HOME"] = str(self.tmp_root / "home")
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("AGENT_HOME", None)
        self.addCleanup(self._restore_environ)
        self.peer_message = _load_hyphenated(
            "peer_message_under_test_d1", "utilities/peer-message.py")
        self.peer_steward = _load_hyphenated(
            "peer_steward_under_test_d1", "utilities/peer-steward.py")

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _record_args(self, harness, **overrides):
        args = dict(
            from_harness=harness, from_session_id=f"sid-{harness}", from_project="proj",
            to_harness="claude", to_session_id=None, to_name="peer-1",
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=["r1", "r2"], body_file=None, body_stdin=False,
        )
        args.update(overrides)
        return self.peer_message.argparse.Namespace(**args)


class RecordFormatTest(_PeerLedgerDirMixin, unittest.TestCase):
    def test_sent_record_has_required_from_fields_and_top_level_ref_keys(self):
        for h in HARNESSES:
            with self.subTest(harness=h):
                ns = self._record_args(h, from_name="peer-name", ref=["r1", "r2"])
                self.assertEqual(self.peer_message.cmd_record(ns), 0)
                path = self.peer_message._ledger_path(f"sid-{h}")
                rec = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(rec["from"]["harness"], h)
                self.assertEqual(rec["from"]["session_id"], f"sid-{h}")
                self.assertEqual(rec["from"]["project"], "proj")
                self.assertNotIn("ref=", rec["from"].get("name", ""))
                self.assertIsInstance(rec["refs"], list)
                self.assertEqual(rec["refs"], ["r1", "r2"])
                self.assertNotIn("transfer_ref", rec)

    def test_transfer_ref_is_a_separate_top_level_key(self):
        ns = self._record_args(
            "codex", from_name="peer-name",
            transfer_ref="deadbeefdeadbeefdeadbeefdeadbeef")
        self.assertEqual(self.peer_message.cmd_record(ns), 0)
        path = self.peer_message._ledger_path("sid-codex")
        rec = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["transfer_ref"], "deadbeefdeadbeefdeadbeefdeadbeef")
        self.assertNotIn("ref=", rec["from"].get("name", ""))


class RefTrailerCleanupTest(unittest.TestCase):
    """C-2: the writer never appends a `ref=` suffix to `from.name` (covered above);
    this is the read-side tolerance for the old records that still carry one."""

    def test_old_ref_suffix_is_stripped_for_display(self):
        dirty = "hearting-46 ; ref=deadbeefdeadbeefdeadbeefdeadbeef"
        self.assertEqual(peer_messages._clean_from_name(dirty), "hearting-46")

    def test_name_without_a_suffix_is_unchanged(self):
        self.assertEqual(peer_messages._clean_from_name("hearting-46"), "hearting-46")

    def test_non_string_name_passes_through(self):
        self.assertIsNone(peer_messages._clean_from_name(None))


class FourPathsShareOneRootTest(_PeerLedgerDirMixin, unittest.TestCase):
    def test_ledger_transfer_steward_and_watch_paths_share_the_canonical_root(self):
        canonical = str(self.peer_message.peer_state_root())
        self.assertTrue(str(self.peer_message._ledger_path("sid-x")).startswith(canonical))
        self.assertTrue(
            str(self.peer_message._transfer_path("a" * 32)).startswith(canonical))
        for h in HARNESSES:
            with self.subTest(harness=h):
                self.assertTrue(
                    str(self.peer_message.steward_marker_path(h, "sid-x")).startswith(canonical))
        self.assertTrue(str(self.peer_steward._watch_root()).startswith(canonical))

    def test_state_roots_index_zero_is_the_canonical_root(self):
        canonical = str(self.peer_message.peer_state_root())
        self.assertEqual(peer_messages._state_roots()[0], canonical)


# --------------------------------------------------------------------------
# Surface ③ — refresh-pump recovery (§7.2, already-landed shape only)
# --------------------------------------------------------------------------

_HEALTH_KEYS = {"state", "last_success_at", "age", "last_error", "leaked_workers", "stall_after"}
_STATES = {"idle", "running", "stalled", "failed"}


class _FakeClock:
    def __init__(self, start=0.0):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, delta):
        self._now += delta


class _HungThread:
    """Never finishes -- simulates a worker stuck past stall_after."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target = target

    def start(self):
        pass

    def is_alive(self):
        return True


class _SyncThread:
    """Runs target synchronously in start() -- deterministic failure-mode tests
    with no real background thread."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)

    def is_alive(self):
        return False


class RefreshRecoveryContract(unittest.TestCase):
    def test_health_keys_are_exactly_the_declared_six(self):
        pump = refresh.RefreshPump(lambda: None, 1.0, clock=_FakeClock())
        self.assertEqual(set(pump.health().keys()), _HEALTH_KEYS)

    def test_idle_before_any_request(self):
        pump = refresh.RefreshPump(lambda: None, 1.0, clock=_FakeClock())
        health = pump.health()
        self.assertEqual(health["state"], "idle")
        self.assertEqual(set(health.keys()), _HEALTH_KEYS)

    def test_blocking_past_stall_after_is_stalled_and_keys_stay_the_same(self):
        clock = _FakeClock(0.0)
        pump = refresh.RefreshPump(lambda: None, 1.0, clock=clock,
                                    thread_factory=_HungThread, stall_after=10.0)
        self.assertTrue(pump.request(force=True))
        clock.advance(11.0)
        health = pump.health()
        self.assertEqual(health["state"], "stalled")
        self.assertEqual(set(health.keys()), _HEALTH_KEYS)

    def test_running_before_the_stall_threshold(self):
        clock = _FakeClock(0.0)
        pump = refresh.RefreshPump(lambda: None, 1.0, clock=clock,
                                    thread_factory=_HungThread, stall_after=10.0)
        pump.request(force=True)
        clock.advance(1.0)
        self.assertEqual(pump.health()["state"], "running")

    def test_systemexit_is_failed_and_keys_stay_the_same(self):
        def producer():
            raise SystemExit("bye")

        pump = refresh.RefreshPump(producer, 1.0, clock=_FakeClock(), thread_factory=_SyncThread)
        pump.request(force=True)
        health = pump.health()
        self.assertEqual(health["state"], "failed")
        self.assertEqual(set(health.keys()), _HEALTH_KEYS)

    def test_plain_exception_is_failed_and_keys_stay_the_same(self):
        def producer():
            raise RuntimeError("boom")

        pump = refresh.RefreshPump(producer, 1.0, clock=_FakeClock(), thread_factory=_SyncThread)
        pump.request(force=True)
        health = pump.health()
        self.assertEqual(health["state"], "failed")
        self.assertEqual(set(health.keys()), _HEALTH_KEYS)

    def test_observed_states_are_exactly_the_declared_enum(self):
        observed = set()
        clock = _FakeClock(0.0)
        idle_pump = refresh.RefreshPump(lambda: None, 1.0, clock=clock)
        observed.add(idle_pump.health()["state"])

        hung = refresh.RefreshPump(lambda: None, 1.0, clock=clock,
                                    thread_factory=_HungThread, stall_after=10.0)
        hung.request(force=True)
        clock.advance(1.0)
        observed.add(hung.health()["state"])          # running
        clock.advance(20.0)
        observed.add(hung.health()["state"])          # stalled

        def boom():
            raise RuntimeError("boom")

        failed = refresh.RefreshPump(boom, 1.0, clock=_FakeClock(), thread_factory=_SyncThread)
        failed.request(force=True)
        observed.add(failed.health()["state"])         # failed

        self.assertEqual(observed, _STATES)

    def test_init_takes_no_harness_parameter(self):
        params = inspect.signature(refresh.RefreshPump.__init__).parameters
        self.assertNotIn("harness", params)

    def test_health_is_a_pure_read_leaked_workers_does_not_grow_across_repeated_reads(self):
        clock = _FakeClock(0.0)
        pump = refresh.RefreshPump(lambda: None, 1.0, clock=clock,
                                    thread_factory=_HungThread, stall_after=10.0)
        pump.request(force=True)
        clock.advance(11.0)
        for _ in range(5):
            health = pump.health()
            self.assertEqual(health["state"], "stalled")
            self.assertEqual(health["leaked_workers"], 0)


# --------------------------------------------------------------------------
# Surface ④ — route chain writer coverage (F-<next>, plan §3 C-5)
# --------------------------------------------------------------------------

class RouteChainWriterParityTest(unittest.TestCase):
    """`WRITER_SUPPORT` is explicit per harness, never skipped. All three are `env`:
    each exports its own native session id to the process, so a ledger line is anchored
    directly and no harness is exempt. claude/codex/opencode produce the same assembled
    chain shape from the same fixture."""

    def test_writer_support_matches_the_declared_map(self):
        self.assertEqual(route_chain.WRITER_SUPPORT,
                          {"claude": "env", "codex": "env", "opencode": "env"})
        for h in HARNESSES:
            with self.subTest(harness=h):
                self.assertIn(h, route_chain.WRITER_SUPPORT)

    def _session(self, harness, sid):
        return Session(harness=harness, pid=1, session_id=sid)

    def test_opencode_session_gets_the_same_chain_shape_as_the_env_writers(self):
        # OpenCode exports its own session id (OPENCODE_SESSION_ID) and the collector
        # already carries it on the row, so a ledger line is anchored exactly as it is for
        # claude/codex. Previously the harness was exempt in WRITER_SUPPORT, which dropped
        # it in enrich() before its own lines were ever read. The same fixture must now
        # assemble into the same chain for opencode as for a claude writer.
        record = {"route_id": "rt-1", "route_hash": "h1", "capability": "autopilot-research",
                  "capability_mode": "default", "effective_intensity": "standard"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FLEET_ROUTE_CHAIN_DIR": tmp}):
                chains = {}
                for harness in ("claude", "opencode"):
                    harness_dir = os.path.join(tmp, harness)
                    os.makedirs(harness_dir, mode=0o700)
                    line = route_chain.build_line(
                        dict(record), event="compose", harness=harness, session_id="sid-1",
                        route_file="/tmp/rt-1.json",
                    )
                    with open(os.path.join(harness_dir, "sid-1.jsonl"), "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(line) + "\n")
                    session = self._session(harness, "sid-1")
                    with mock.patch("fleet.route.load", return_value=record), \
                         mock.patch("fleet.route.load_outcome",
                                    return_value={"present": False}):
                        route_chain.enrich([session])
                    self.assertIsNotNone(session.route_chain,
                                         f"{harness} session got no route_chain at all")
                    chains[harness] = session.route_chain
                self.assertEqual(chains["opencode"]["key"], chains["claude"]["key"])
                self.assertEqual(chains["opencode"]["visible"], chains["claude"]["visible"])
                self.assertEqual([n["label"] for n in chains["opencode"]["nodes"]],
                                 [n["label"] for n in chains["claude"]["nodes"]])
                self.assertEqual([n["route_id"] for n in chains["opencode"]["nodes"]],
                                 [n["route_id"] for n in chains["claude"]["nodes"]])
                self.assertEqual([n["state"] for n in chains["opencode"]["nodes"]],
                                 [n["state"] for n in chains["claude"]["nodes"]])

    def test_claude_and_codex_produce_the_same_chain_shape_from_the_same_fixture(self):
        record = {"route_id": "rt-1", "route_hash": "h1", "capability": "autopilot-research",
                  "capability_mode": "default", "effective_intensity": "standard"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FLEET_ROUTE_CHAIN_DIR": tmp}):
                chains = {}
                for harness in ("claude", "codex"):
                    line = route_chain.build_line(
                        record, event="compose", harness=harness, session_id="sid-1",
                        route_file="/tmp/rt-1.json",
                    )
                    route_chain.append(harness, "sid-1", line)
                    session = self._session(harness, "sid-1")
                    with mock.patch("fleet.route.load", side_effect=lambda *a, **k: record), \
                         mock.patch("fleet.route.load_outcome",
                                     side_effect=lambda *a, **k: {"present": False}):
                        route_chain.enrich([session])
                    chains[harness] = session.route_chain
                self.assertEqual(
                    [n["label"] for n in chains["claude"]["nodes"]],
                    [n["label"] for n in chains["codex"]["nodes"]],
                )
                self.assertEqual(
                    [n["state"] for n in chains["claude"]["nodes"]],
                    [n["state"] for n in chains["codex"]["nodes"]],
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
