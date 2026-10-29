"""Tap-based sid recovery + neighbor ai-title theft guard (2026-07-20 fallback-name incident).

Root-cause pair, both fixed here and pinned by these tests:
  (a) `~/.claude/sessions/<pid>.json` registry rows can vanish while their process lives,
      leaving interactive rows sid-less — the statusline tap (§5) now carries the owning
      claude pid + /proc starttime, and enrich() recovers the sid from it (F-25 tier-2);
  (b) a sid-less row borrows the NEWEST neighbor .jsonl in the project dir for liveness and
      also adopted its ai-title — every registry-less same-cwd row collapsed onto one stale
      title. Native ai-title adoption is now disabled even for the owning sid;
      successful Fleet sidecars alone provide generated session subjects.

Hermetic: temp CLAUDE_CONFIG_DIR only; no real ~/.claude, no live process dependency.
The statusline.sh injection tests drive the real script with a verified native executable fixture.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from test_process_identity import FakeClaudeProc

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.collectors import claude    # noqa: E402
from fleet.model import Session        # noqa: E402

def _find_statusline(start):
    """Repo-root statusline.sh from either home: canonical (adapters/claude/tools) needs
    three hops up, the byte-identical mirror (tools/) needs one — walk until it appears."""
    d = start
    for _ in range(6):
        cand = os.path.join(d, "adapters", "claude", "statusline.sh")
        if os.path.exists(cand):
            return cand
        d = os.path.dirname(d)
    return ""


_STATUSLINE = _find_statusline(_TOOLS_DIR)


class _HomeMixin:
    """Temp CLAUDE_CONFIG_DIR with helpers for registry/tap/transcript fixtures."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "FLEET_TITLE_STATE_DIR")}
        self.home = os.path.join(self._tmp.name, "claude")
        os.makedirs(self.home)
        os.environ["CLAUDE_CONFIG_DIR"] = self.home
        os.environ["FLEET_TITLE_STATE_DIR"] = os.path.join(self._tmp.name, "titles")

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _transcript(self, cwd, sid, ai_title=None):
        proj = os.path.join(self.home, "projects", claude._enc_cwd(cwd))
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": "hello"}) + "\n")
            if ai_title:
                f.write(json.dumps({"type": "ai-title", "aiTitle": ai_title}) + "\n")
        return path

    def _tap(self, sid, payload, mtime=None):
        sldir = os.path.join(self.home, ".statusline")
        os.makedirs(sldir, exist_ok=True)
        path = os.path.join(sldir, sid + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _registry(self, pid, payload):
        rdir = os.path.join(self.home, "sessions")
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, "%d.json" % pid), "w", encoding="utf-8") as f:
            json.dump(payload, f)


class TitleTheftGuardTest(_HomeMixin, unittest.TestCase):
    """(b) — a sid-less row must not wear a neighbor transcript's ai-title."""

    def test_sidless_row_never_adopts_neighbor_ai_title(self):
        self._transcript("/proj/x", "neighbor-sid", ai_title="Stolen Neighbor Title")
        s = Session(harness="claude", pid=999942, cwd="/proj/x", slug="x")
        claude.enrich(s)
        self.assertIsNone(s.title)                 # a title gap stays blank in Fleet

    def test_sidless_row_does_not_borrow_mtime_for_liveness(self):
        """A neighboring transcript cannot establish this process's activity."""
        self._transcript("/proj/x", "neighbor-sid", ai_title="Stolen Neighbor Title")
        s = Session(harness="claude", pid=999942, cwd="/proj/x", slug="x")
        claude.enrich(s)
        self.assertIsNone(s.mtime)

    def test_own_transcript_ai_title_is_not_a_fleet_title(self):
        """Knowing the sid cannot turn a native title into a Fleet subject."""
        self._transcript("/proj/x", "own-sid", ai_title="Own Honest Title")
        s = Session(harness="claude", pid=999942, cwd="/proj/x", slug="x", session_id="own-sid")
        claude.enrich(s)
        self.assertIsNone(s.title)


class TapSidRecoveryTest(_HomeMixin, unittest.TestCase):
    """(a) — registry silence + matching (pid, proc_start) tap recovers the sid."""

    def _sess(self, **over):
        base = dict(harness="claude", pid=994242, cwd="/proj/y", slug="y")
        base.update(over)
        s = Session(**{k: v for k, v in base.items() if k in ("harness", "pid", "cwd", "slug", "session_id")})
        s.proc_start = over.get("proc_start", "777")
        return s

    def test_recovers_sid_without_adopting_native_title(self):
        self._transcript("/proj/y", "own-sid", ai_title="Recovered Own Title")
        self._tap("own-sid", {"session_id": "own-sid", "pid": 994242, "proc_start": "777"})
        s = self._sess()
        claude.enrich(s)
        self.assertEqual(s.session_id, "own-sid")
        self.assertIsNone(s.title)

    def test_proc_start_mismatch_refuses(self):
        """PID reuse: same pid, different starttime → the tap belongs to a dead session."""
        self._tap("own-sid", {"session_id": "own-sid", "pid": 994242, "proc_start": "778"})
        s = self._sess()
        claude.enrich(s)
        self.assertIsNone(s.session_id)

    def test_tap_without_proc_start_refuses(self):
        self._tap("own-sid", {"session_id": "own-sid", "pid": 994242})
        s = self._sess()
        claude.enrich(s)
        self.assertIsNone(s.session_id)

    def test_row_without_proc_start_refuses(self):
        """No /proc identity on our side either → cannot verify, so do not guess."""
        self._tap("own-sid", {"session_id": "own-sid", "pid": 994242, "proc_start": "777"})
        s = self._sess(proc_start=None)
        claude.enrich(s)
        self.assertIsNone(s.session_id)

    def test_unrelated_registry_and_tap_claims_conflict(self):
        """Exact but unrelated claims do not select a current conversation."""
        self._registry(994242, {"pid": 994242, "sessionId": "registry-sid"})
        self._tap("tap-sid", {"session_id": "tap-sid", "pid": 994242, "proc_start": "777"})
        s = self._sess()
        claude.enrich(s)
        self.assertIsNone(s.session_id)

    def test_unrelated_matching_taps_cannot_be_ordered_by_mtime(self):
        self._tap("old-sid", {"session_id": "old-sid", "pid": 994242, "proc_start": "777"},
                  mtime=1000.0)
        self._tap("new-sid", {"session_id": "new-sid", "pid": 994242, "proc_start": "777"},
                  mtime=2000.0)
        s = self._sess()
        claude.enrich(s)
        self.assertIsNone(s.session_id)

    def test_malformed_tap_is_silence(self):
        sldir = os.path.join(self.home, ".statusline")
        os.makedirs(sldir, exist_ok=True)
        with open(os.path.join(sldir, "bad.json"), "w") as f:
            f.write("{not json")
        s = self._sess()
        claude.enrich(s)                            # must not raise
        self.assertIsNone(s.session_id)

    def test_missing_tap_dir_is_silence(self):
        s = self._sess()
        claude.enrich(s)
        self.assertIsNone(s.session_id)

    def test_recovered_sid_feeds_telemetry_and_sidecar_steps(self):
        """The recovery lands BEFORE step 2, so the tap telemetry itself is consumed."""
        self._transcript("/proj/y", "own-sid")
        self._tap("own-sid", {"session_id": "own-sid", "pid": 994242, "proc_start": "777",
                              "cost": {"total_cost_usd": 1.25}})
        s = self._sess()
        claude.enrich(s)
        self.assertEqual(s.session_id, "own-sid")
        self.assertEqual(s.cost, 1.25)


class StatuslinePriorClaimTest(unittest.TestCase):
    """Real shell writes and actual Fleet reader across registry-loss events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f = FakeClaudeProc(self.root)
        self.f.process()
        self.agent = self.root / "agent"
        (self.agent / "utilities").mkdir(parents=True)
        source = Path(_STATUSLINE).resolve().parents[2]
        (self.agent / "utilities/claude_process_identity.py").write_text(
            "import importlib.util\n"
            f"s=importlib.util.spec_from_file_location('native_helper', {str(source / 'utilities/claude_process_identity.py')!r})\n"
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
            "def statusline_identity(parent, sid, home):\n"
            f"    return m.statusline_identity(71, sid, home, proc_root={str(self.f.root)!r})\n"
            "def statusline_bindings(parent, sid, home):\n"
            f"    return m.statusline_bindings(71, sid, home, proc_root={str(self.f.root)!r})\n")
        # Record the PID delivered to the display helper, independently of the
        # PID preserved in the telemetry tap. A retained claim is not a verdict.
        self.display = self.root / "display-pid.json"
        (self.agent / "tools/fleet").mkdir(parents=True)
        (self.agent / "tools/fleet/session_handle.py").write_text(
            "import json\n"
            "def resolve_display_inputs(harness, sid, pid=None):\n"
            f"    open({str(self.display)!r}, 'w').write(json.dumps(pid))\n"
            "    return {}\n")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("read_proc_start", "101"), ("_comm_of", "8.7.6"),
                            ("_read_cwd", (self.tmp.name, False))):
            self.stack.enter_context(mock.patch.object(claude.procscan, name, return_value=value))

    def reader(self):
        return claude.session_id_of_process(71, home=str(self.f.home))

    def event(self, *, expect_tap_update=True):
        result = subprocess.run(["bash", _STATUSLINE], input=json.dumps({
            "session_id": "current", "cwd": str(self.root), "cost": {"total_cost_usd": 2.25},
            "pid": 9999, "proc_start": "9999"}), capture_output=True, text=True, timeout=30,
            env={**os.environ, "HOME": str(self.root), "AGENT_HOME": str(self.agent),
                 "CLAUDE_CONFIG_DIR": str(self.f.home), "FLEET_TITLE_DISABLE": "1",
                 "HARNESS_CAPACITY_REFRESH_DISABLE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "statusline must still render")
        latest = json.loads((self.f.home / ".statusline-last.json").read_text())
        self.assertEqual(latest["cost"]["total_cost_usd"], 2.25)
        tap = json.loads((self.f.home / ".statusline/current.json").read_text())
        if expect_tap_update:
            self.assertEqual(tap["cost"]["total_cost_usd"], 2.25)
            self.assertIn("hearting_context_request", tap)
        return tap

    def test_conflict_survives_real_shell_overwrite_without_becoming_old_sid(self):
        for selector in (None, "old", "current"):
            with self.subTest(selector=selector):
                self.f.process(argv=["claude"] + (["--session-id", selector] if selector else []))
                self.f.tap(sid="old")
                self.f.tap()
                old = (self.f.home / ".statusline/old.json").read_bytes()
                self.assertIsNone(self.reader())
                tap = self.event()
                self.assertIsNone(self.reader())
                self.assertEqual((tap["pid"], tap["proc_start"]), (71, "101"))
                self.assertEqual((self.f.home / ".statusline/old.json").read_bytes(), old)
                self.assertIsNone(json.loads(self.display.read_text()))

    def test_unobserved_callback_leaves_both_conflicting_raw_claims_untouched(self):
        proxy = self.agent / "utilities/claude_process_identity.py"
        original = proxy.read_text()
        for mode in ("unknown-argv", "helper-raises", "helper-import-unavailable"):
            with self.subTest(mode=mode):
                self.f.process(argv=["unrecognized-runtime-title"] if mode == "unknown-argv" else ["claude"])
                self.f.tap(sid="old")
                self.f.tap()
                paths = {sid: self.f.home / ".statusline" / (sid + ".json") for sid in ("old", "current")}
                # Even expiry housekeeping must not turn a no-write decision
                # into deletion of either side of the same conflict.
                for path in paths.values():
                    os.utime(path, (1000, 1000))
                before = {sid: path.read_bytes() for sid, path in paths.items()}
                for sid, raw in before.items():
                    self.assertEqual(json.loads(raw), {"session_id": sid, "pid": 71, "proc_start": "101"})
                self.assertEqual(list((self.f.home / "sessions").iterdir()), [])
                self.assertIsNone(self.reader())
                if mode == "helper-raises":
                    proxy.write_text(original + "\ndef statusline_bindings(*args, **kwargs):\n"
                                     "    raise RuntimeError('private helper lookup failure')\n")
                elif mode == "helper-import-unavailable":
                    proxy.write_text("raise ImportError('private helper unavailable')\n")
                try:
                    tap = self.event(expect_tap_update=False)
                finally:
                    proxy.write_text(original)
                self.assertIsNone(self.reader())
                for sid, path in paths.items():
                    self.assertEqual(path.read_bytes(), before[sid])
                    self.assertEqual(path.stat().st_mtime, 1000)
                self.assertEqual((tap["pid"], tap["proc_start"]), (71, "101"))
                self.assertNotEqual(tap["pid"], 9999)  # stdin is never new evidence
                self.assertIsNone(json.loads(self.display.read_text()))
                self.assertEqual(list((self.f.home / "sessions").iterdir()), [])

    def test_unobserved_callback_without_prior_claim_writes_telemetry_without_pid(self):
        self.f.process(argv=["unrecognized-runtime-title"], parent=72)
        self.f.process(72)
        self.f.registry(72)
        tap = self.event()
        self.assertNotIn("pid", tap)
        self.assertNotIn("proc_start", tap)
        self.assertIsNone(self.reader())
        self.assertIsNone(json.loads(self.display.read_text()))

    def test_unreadable_current_prior_preserves_raw_conflict(self):
        proxy = self.agent / "utilities/claude_process_identity.py"
        original = proxy.read_text()
        for argv in (["claude"], ["unrecognized-runtime-title"]):
            for fault in ("oversize", "denied", "replaced"):
                with self.subTest(argv=argv, fault=fault):
                    self.f.process(argv=argv)
                    self.f.tap(sid="old")
                    self.f.tap()
                    path = self.f.home / ".statusline/current.json"
                    if fault == "oversize":
                        row = json.loads(path.read_text())
                        path.write_text(json.dumps({**row, "padding": "x" * 65536}))
                    elif fault == "denied":
                        proxy.write_text(original +
                            "\n_real_claim = m._focused_tap_claim\n"
                            "def denied_claim(path, pid, start):\n"
                            "    if path.name == 'current.json':\n"
                            "        raise PermissionError('private exact-prior read denied')\n"
                            "    return _real_claim(path, pid, start)\n"
                            "m._focused_tap_claim = denied_claim\n")
                    else:
                        proxy.write_text(original +
                            f"\n_prior = m.Path({str(path)!r})\n"
                            "_real_read = m.os.read\n"
                            "def replaced_read(fd, size):\n"
                            "    prior = m.os.fstat(fd).st_ino == _prior.stat().st_ino\n"
                            "    raw = _real_read(fd, size)\n"
                            "    if prior:\n"
                            "        info = _prior.stat()\n"
                            "        replacement = _prior.with_suffix('.new')\n"
                            "        replacement.write_bytes(raw)\n"
                            "        m.os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))\n"
                            "        m.os.replace(replacement, _prior)\n"
                            "    return raw\n"
                            "m.os.read = replaced_read\n")
                    paths = [path, path.with_name("old.json")]
                    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
                    self.assertIsNone(self.reader())
                    try:
                        self.event(expect_tap_update=False)
                    finally:
                        proxy.write_text(original)
                    self.assertIsNone(self.reader())
                    self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in paths], before)
                    self.assertIsNone(json.loads(self.display.read_text()))
                    self.assertEqual(list((self.f.home / "sessions").iterdir()), [])

    def test_native_read_denied_does_not_discard_prior_claims(self):
        proxy = self.agent / "utilities/claude_process_identity.py"
        original = proxy.read_text()
        for argv in (["claude"], ["unrecognized-runtime-title"]):
            with self.subTest(argv=argv):
                self.f.process(argv=argv)
                native = self.f.registry()
                self.f.tap(sid="old")
                self.f.tap()
                paths = [self.f.home / ".statusline" / (sid + ".json") for sid in ("old", "current")]
                before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
                native_raw = native.read_bytes()
                # The actual reader can independently read the valid native
                # record. Only this callback's bounded observation is denied.
                self.assertEqual(self.reader(), "current")
                proxy.write_text(original +
                    f"\n_native = m.Path({str(native)!r})\n"
                    "_real_open = m.os.open\n"
                    "def denied_open(path, *args, **kwargs):\n"
                    "    if m.Path(path) == _native:\n"
                    "        raise PermissionError('private native read denied')\n"
                    "    return _real_open(path, *args, **kwargs)\n"
                    "m.os.open = denied_open\n")
                try:
                    self.event(expect_tap_update=False)
                finally:
                    proxy.write_text(original)
                self.assertEqual(self.reader(), "current")
                self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in paths], before)
                self.assertEqual(native.read_bytes(), native_raw)
                self.assertIsNone(json.loads(self.display.read_text()))

    def test_retained_current_claim_cannot_expire_aged_conflicting_claim(self):
        self.f.tap(sid="old")
        self.f.tap()
        old = self.f.home / ".statusline/old.json"
        os.utime(old, (1000, 1000))
        before = old.read_bytes(), old.stat().st_mtime_ns
        self.assertIsNone(self.reader())
        tap = self.event()
        self.assertEqual((tap["pid"], tap["proc_start"]), (71, "101"))
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns), before)
        self.assertIsNone(self.reader())
        self.assertIsNone(json.loads(self.display.read_text()))
        self.assertEqual(list((self.f.home / "sessions").iterdir()), [])

    def test_priorless_unknown_telemetry_cannot_expire_unrelated_conflict(self):
        self.f.process(argv=["unrecognized-runtime-title"])
        self.f.tap(sid="old")
        self.f.tap(sid="other")
        paths = [self.f.home / ".statusline" / (sid + ".json") for sid in ("old", "other")]
        os.utime(paths[0], (1000, 1000))
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        self.assertIsNone(self.reader())
        tap = self.event()
        self.assertNotIn("pid", tap)
        self.assertNotIn("proc_start", tap)
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in paths], before)
        self.assertIsNone(self.reader())
        self.assertIsNone(json.loads(self.display.read_text()))
        self.assertEqual(list((self.f.home / "sessions").iterdir()), [])

    def test_verified_current_callback_keeps_normal_cleanup_and_telemetry(self):
        self.f.registry()
        self.f.tap(sid="old", start="100")
        old = self.f.home / ".statusline/old.json"
        os.utime(old, (1000, 1000))
        leftover = old.with_name(".old.json.tmp")
        leftover.write_text("partial old write")
        os.utime(leftover, (1000, 1000))
        self.assertEqual(self.reader(), "current")
        tap = self.event()
        self.assertEqual((tap["pid"], tap["proc_start"]), (71, "101"))
        self.assertEqual(json.loads(self.display.read_text()), 71)
        self.assertFalse(old.exists())
        self.assertFalse(leftover.exists())
        self.assertEqual(self.reader(), "current")

    def test_current_prior_tap_survives_historical_selector(self):
        self.f.process(argv=["claude", "--session-id=old"])
        self.f.tap()
        self.assertEqual(self.reader(), "current")
        tap = self.event()
        self.assertEqual(self.reader(), "current")
        self.assertEqual((tap["pid"], tap["proc_start"]), (71, "101"))

    def test_foreign_exact_tap_is_not_overruled_by_callback_selector(self):
        self.f.process(argv=["claude", "--session-id=current"])
        self.f.tap(sid="foreign")
        self.assertEqual(self.reader(), "foreign")
        tap = self.event()
        self.assertEqual(self.reader(), "foreign")
        self.assertNotIn("pid", tap)
        self.assertNotIn("proc_start", tap)

    def test_invalid_prior_claims_are_not_copied(self):
        proxy = self.agent / "utilities/claude_process_identity.py"
        original = proxy.read_text()
        for case in ("reused", "other-pid", "nearer", "service", "foreign-current",
                     "malformed-current", "unknown-reused", "unknown-other-pid",
                     "unknown-foreign-current", "foreign-owner", "foreign-tap",
                     "stale-current", "other-pid-current", "foreign-native-owner"):
            with self.subTest(case=case):
                proxy.write_text(original)
                (self.f.home / "sessions/71.json").unlink(missing_ok=True)
                self.f.process()
                self.f.tap(start="100" if case in {"reused", "unknown-reused"} else "101",
                           pid=72 if case in {"other-pid", "nearer", "unknown-other-pid"} else 71)
                if case == "nearer":
                    self.f.process(71, parent=72)
                    self.f.process(72)
                elif case == "service":
                    self.f.process(argv=["claude", "daemon", "run"])
                elif case.startswith("unknown-"):
                    self.f.process(argv=["unrecognized-runtime-title"])
                    if case == "unknown-foreign-current":
                        self.f.registry(sid="foreign")
                elif case == "foreign-current":
                    self.f.registry(sid="foreign")
                elif case == "malformed-current":
                    self.f.registry().write_text("{")
                elif case == "stale-current":
                    self.f.registry(start="100")
                elif case == "other-pid-current":
                    path = self.f.registry()
                    row = json.loads(path.read_text())
                    path.write_text(json.dumps({**row, "pid": 72}))
                elif case in {"foreign-tap", "foreign-native-owner"}:
                    if case == "foreign-native-owner":
                        self.f.registry()
                    proxy = self.agent / "utilities/claude_process_identity.py"
                    with proxy.open("a") as handle:
                        handle.write("\n_original_uid = m.os.getuid()\n"
                                     "m.os.getuid = lambda: _original_uid + 1\n")
                elif case == "foreign-owner":
                    proxy = self.agent / "utilities/claude_process_identity.py"
                    with proxy.open("a") as handle:
                        handle.write("\nm.os.geteuid = lambda: m.os.getuid() + 1\n")
                tap = self.event()
                self.assertNotIn("pid", tap)
                self.assertNotIn("proc_start", tap)


@unittest.skipUnless(os.path.exists(_STATUSLINE) and os.path.isdir("/proc") and shutil.which("cc"),
                     "needs adapters/claude/statusline.sh and /proc")
class StatuslineTapInjectionTest(unittest.TestCase):
    """Real shell + native executable/argv/PID/start + private current registry."""

    @classmethod
    def setUpClass(cls):
        cls.native = tempfile.TemporaryDirectory()
        root = Path(cls.native.name)
        source = root / "runtime.c"
        source.write_text(r'''#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
int main(void) {
    char buffer[8192], path[8192];
    snprintf(path, sizeof(path), "/proc/%d/stat", getpid());
    FILE *f = fopen(path, "r");
    if (!f || !fgets(buffer, sizeof(buffer), f)) return 2;
    fclose(f);
    char *token = strtok(strrchr(buffer, ')') + 2, " ");
    for (int i=0; i<19 && token; i++) token = strtok(NULL, " ");
    if (!token) return 3;
    snprintf(path, sizeof(path), "%s/sessions/%d.json", getenv("CLAUDE_CONFIG_DIR"), getpid());
    if (!getenv("FIXTURE_NO_REGISTRY")) {
        f = fopen(path, "w");
        if (!f) return 4;
        fprintf(f, "{\"pid\":%d,\"procStart\":\"%s\",\"sessionId\":\"%s\","
                "\"name\":\"%s\",\"nameSource\":\"%s\"}", getpid(), token, getenv("FIXTURE_SID"),
                getenv("FIXTURE_NAME") ? getenv("FIXTURE_NAME") : "",
                getenv("FIXTURE_NAME_SOURCE") ? getenv("FIXTURE_NAME_SOURCE") : "derived");
        fclose(f);
    }
    pid_t child = fork();
    if (child == 0) { execl("/bin/bash", "bash", getenv("FIXTURE_STATUSLINE"), NULL); _exit(5); }
    int status; waitpid(child, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 6;
}
''')
        cls.binary = root / "claude" / "versions" / "8.7.6"
        cls.binary.parent.mkdir(parents=True)
        try:
            subprocess.run([shutil.which("cc"), str(source), "-o", str(cls.binary)],
                           check=True, capture_output=True, timeout=30)
        except BaseException:
            cls.native.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.native.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agent_home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.agent_home)
        # The tap lives in the Claude config home (what tools/fleet reads), never
        # under AGENT_HOME: a managed release tree written to goes cache-stale.
        self.config_home = os.path.join(self._tmp.name, "claude-config")
        os.makedirs(self.config_home)
        os.makedirs(os.path.join(self.config_home, "sessions"))
        self.stub = str(self.binary)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, stdin_json, extra_env=None):
        env = dict(os.environ)
        for name in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                     "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"):
            env.pop(name, None)
        env["AGENT_HOME"] = self.agent_home
        env["CLAUDE_CONFIG_DIR"] = self.config_home
        env["FLEET_TITLE_DISABLE"] = "1"           # keep the refresher out of the test
        env["HARNESS_CAPACITY_REFRESH_DISABLE"] = "1"   # and the capacity gauge probe (network + live cache)
        env["FIXTURE_STATUSLINE"] = _STATUSLINE
        env["FIXTURE_SID"] = json.loads(stdin_json)["session_id"]
        env.update(extra_env or {})
        proc = subprocess.Popen([self.stub], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out, err = proc.communicate(stdin_json.encode("utf-8"), timeout=30)
        return proc, out, err

    def _tap(self, sid):
        with open(os.path.join(self.config_home, ".statusline", sid + ".json"),
                  encoding="utf-8") as f:
            return f.read()

    def test_tap_never_lands_under_agent_home(self):
        # 2026-09-16: a session launched with AGENT_HOME=<managed release tree>
        # wrote .statusline* there, drifting the release digest so all three
        # runtimes reported freshness=cache-stale. State goes to the config home.
        stdin_json = json.dumps({"session_id": "sidH", "cwd": "/tmp",
                                 "model": {"display_name": "M"}})
        self._run(stdin_json)
        self.assertTrue(os.path.isfile(os.path.join(self.config_home, ".statusline", "sidH.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.config_home, ".statusline-last.json")))
        for name in (".statusline", ".statusline-last.json", ".statusline-last-out.txt"):
            self.assertFalse(os.path.lexists(os.path.join(self.agent_home, name)), name)

    def test_tap_gains_pid_and_proc_start(self):
        stdin_json = json.dumps({"session_id": "sidZ", "cwd": "/tmp",
                                 "model": {"display_name": "M"}})
        proc, _out, _err = self._run(stdin_json)
        raw = self._tap("sidZ")
        d = json.loads(raw)                        # valid JSON after injection
        self.assertEqual(d["pid"], proc.pid)       # actual version-named native fixture
        self.assertTrue(str(d["proc_start"]).isdigit())
        self.assertEqual(d["session_id"], "sidZ")  # original fields intact

    def test_unverified_or_foreign_runtime_drops_supplied_pid_evidence(self):
        payload = json.dumps({"session_id": "current", "cwd": "/tmp",
                              "pid": 12345, "proc_start": "54321"})
        for extra in ({"FIXTURE_NO_REGISTRY": "1"}, {"FIXTURE_SID": "foreign"}):
            with self.subTest(extra=extra):
                self._run(payload, extra)
                record = json.loads(self._tap("current"))
                self.assertNotIn("pid", record)
                self.assertNotIn("proc_start", record)
                self.assertIn("hearting_context_request", record)

    def test_failed_identity_helper_keeps_telemetry_without_unverified_pid(self):
        helper = Path(self.agent_home) / "utilities/claude_process_identity.py"
        helper.parent.mkdir()
        helper.write_text("raise OSError('fixture helper unavailable')\n")
        process, _, _ = self._run(json.dumps({"session_id": "helper-failure", "cwd": "/tmp",
                                            "pid": 12345, "proc_start": "54321"}))
        self.assertEqual(process.returncode, 0)
        record = json.loads(self._tap("helper-failure"))
        self.assertNotIn("pid", record)
        self.assertNotIn("proc_start", record)
        self.assertIn("hearting_context_request", record)

    def test_normal_claude_launcher_keeps_exact_pid_evidence(self):
        launcher = Path(self._tmp.name) / "claude"
        shutil.copyfile(self.binary, launcher)
        launcher.chmod(0o700)
        self.stub = str(launcher)
        process, _, _ = self._run(json.dumps({"session_id": "normal", "cwd": "/tmp"}))
        self.assertEqual(json.loads(self._tap("normal"))["pid"], process.pid)

    def test_verified_runtime_displays_user_name_and_derived_name_uses_last_title(self):
        source = Path(_STATUSLINE).resolve().parents[2]
        tools = Path(self.config_home) / "tools"
        tools.mkdir()
        (tools / "fleet").symlink_to(source / "tools/fleet", target_is_directory=True)
        state = Path(self._tmp.name) / "titles"
        (state / "claude").mkdir(parents=True)
        for sid, name_source, name, expected in (
                ("named", "user", "User chosen subject", "User chosen subject"),
                ("derived", "derived", "workspace-42", "Last successful title")):
            (state / "claude" / (sid + ".json")).write_text(json.dumps({
                "title": "Last successful title", "ts": 1, "source": "refresher"}))
            process, output, error = self._run(json.dumps({"session_id": sid, "cwd": "/tmp"}), {
                "AGENT_HOME": self.config_home, "FLEET_TITLE_STATE_DIR": str(state),
                "FIXTURE_NAME": name, "FIXTURE_NAME_SOURCE": name_source})
            self.assertEqual(process.returncode, 0, error)
            self.assertIn(expected, output.decode())
            self.assertEqual(json.loads(self._tap(sid))["pid"], process.pid)

    def test_non_brace_quote_input_stays_verbatim(self):
        stdin_json = '{ "session_id": "sidY", "cwd": "/tmp" }'
        self._run(stdin_json)
        record = json.loads(self._tap("sidY"))
        # Native fields are untouched; the tap only gains Hearting's additive request record.
        self.assertEqual({k: record[k] for k in ("session_id", "cwd")},
                         {"session_id": "sidY", "cwd": "/tmp"})
        self.assertIn("hearting_context_request", record)

    def test_tap_keeps_allowlisted_compaction_request_separate_from_raw_window(self):
        payload = json.dumps({"session_id": "sid-policy", "cwd": "/tmp",
                              "context_window": {"context_window_size": 1000000,
                                                  "used_percentage": 25}})
        self._run(payload, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "750000",
                            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"})
        record = json.loads(self._tap("sid-policy"))
        self.assertEqual(record["context_window"]["context_window_size"], 1000000)
        self.assertEqual(record["hearting_context_request"]["window"], "750000")
        self.assertEqual(record["hearting_context_request"]["pct_override"], "80")

    def test_statusline_does_not_guess_model_window_from_model_id(self):
        payload = json.dumps({"session_id": "sid-no-window", "cwd": "/tmp",
                              "model": {"id": "claude-sonnet-5-5-1m"},
                              "context_window": {"current_usage": {
                                  "input_tokens": 500000,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}})
        _proc, output, _err = self._run(payload)
        self.assertNotIn("context", output.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
