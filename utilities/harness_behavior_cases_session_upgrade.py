#!/usr/bin/env python3
"""Live session-upgrade scenarios for ``harness_behavior_scenarios.py``.

A session that is already open should move to a newly installed Hearting without
the window being closed (the session shell's host hands over to a new host while
the native process keeps its PID), and a session reopened with the same id should
take its open routes back by itself.  These scenarios measure that on the real
Claude, Codex and OpenCode TUIs in the base harness's throwaway HOME, loopback
model and private tmux server.  Nothing here touches the operator's HOME.

``session-upgrade``            idle session; install release B; same native PID, new host
``session-upgrade-scrollback`` the same with 3 screens of output; L1/L3 digests before/after
``session-upgrade-draft``      a draft is in the composer: the switch waits, then happens
``session-upgrade-turn``       a turn is running: the switch waits, then happens
``session-upgrade-question``   an answer question is open (``not-produced`` if the panel never shows it)
``session-upgrade-page``       a panel read page is open (``not-produced`` without a route row)
``session-upgrade-delivery``   a peer delivery is in flight (``not-produced`` without a reference)
``session-upgrade-fail``       the successor throws: the old host stays, nothing is lost
``route-resume``               reopen the same sid: its open routes start, a waiting notice arrives

The real TUIs run only under ``autopilot-lab:host-run``; ``harness_behavior_cases_session_upgrade.test.py``
checks the registration, argv, digest and table pieces offline.

    python3 utilities/harness_behavior_scenarios.py --scenario session-upgrade,route-resume \
        --harness claude,codex,opencode --output-dir DIR
    python3 utilities/harness_behavior_cases_session_upgrade.py --table DIR/results.json

Synthetic data stays under the fixture root.  ``not-produced`` cells are covered by the
synthetic tests only and are never counted as a live pass.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import (ANSI_RE, Reply, Scenario,  # noqa: E402
                                        enable_session_shell, wait_for)
from harness_behavior_cases_b import (OPTION_A, fixture_python,  # noqa: E402
                                      native_question, send_peer, session_id)
from harness_behavior_cases_d import NOTICE_CODE  # noqa: E402

KEY_TEXT = "SCENARIO-UPGRADE-KEY"
SWITCH_WAIT = 30          # seconds the new host may take to appear
DEFER_WAIT = 12           # seconds a held state is watched for a (wrong) switch
MARKER_FILE = "upgrade-fixture-release.txt"
FAIL_ENV = "HEARTING_UPGRADE_FIXTURE_FAIL"
FAIL_LINE = ("if(process.env.%s==='1'&&Number(process.env.HEARTING_SHELL_GENERATION||0)>0)"
             "throw new Error('fixture: successor refuses');\n" % FAIL_ENV)
NOT_PRODUCED = "not-produced"
# What a successor that throws at load writes with node's own error printer.
LOAD_ERROR_MARKS = ("Module._load", "Node.js v", "successor refuses")

# Lines that change with time, not with the session: spinners, clocks, elapsed counters.
VOLATILE = (
    re.compile(r"[⠀-⣿✻✶✳✢✽·●◐◓◑◒]\s*\S.*(?:\d+s\b|esc to interrupt|interrupt)", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    re.compile(r"\(\s*\d+(?:\.\d+)?\s*[smh]\b"),
    re.compile(r"\b\d+(?:\.\d+)?s\s*[·•|]"),
    re.compile(r"\b\d+m \d+s\b|\(\d+s\)|·\s*\d+s\s*$"),
    re.compile(r"ctrl\+b to run in background"),
)
# Animation glyphs: a braille spinner frame, and the moving block of OpenCode's progress bar
# (``■⬝⬝``). Each frame is the same cell to the comparison; the row's text still compares.
ANIMATION = (re.compile(r"[⠀-⣿]"), re.compile(r"[■⬝]"))
ANIMATION_CELL = ("⠿", "⬝")
# The one-line note the host paints while a new release waits for a safe moment, and the
# one it paints after a switch without scrollback.  A held state is expected to show it, so
# the "nothing else changed" comparison drops it (and counts that it was there).
WAIT_NOTE = (re.compile(r"New release waiting|새 판 대기|새 판 기다"),)
VOLATILE_RULE = ("drop rows with a spinner glyph plus an elapsed/interrupt hint, a clock "
                 "(h:mm[:ss]), '(12s' or '(1m 5s)' elapsed, '12s ·', or a background-run hint; trailing "
                 "blanks and ANSI are removed; a held turn also drops Claude's spinner-verb and tip rows and blank rows; braille spinner frames and ■/⬝ progress cells count as one cell each")


# --------------------------------------------------------------------------
# Pure helpers (offline-testable)
# --------------------------------------------------------------------------

def native_part(row: str) -> str:
    """The row without the host's right-hand panel (its box starts at the last left border)."""
    end = row.rstrip()
    if not end or end[-1] not in "│┐┘":
        return end
    body = end[:-1]
    cut = max(body.rfind(char) for char in "│┌└")
    return end if cut < 0 else end[:cut].rstrip()


def screen_rows(text: str, blank=(), native=False) -> list[str]:
    """Screen rows without ANSI; ``blank`` patterns are cut out of a row and the gap closed.

    ``native`` keeps only what the native program paints, left of the host's own panel.
    """
    rows = []
    for line in (text or "").splitlines():
        row = ANSI_RE.sub("", line)
        for rule, cell in zip(ANIMATION, ANIMATION_CELL):
            row = rule.sub(cell, row)
        if native:
            row = native_part(row)
        if blank:
            for rule in blank:
                row = rule.sub("", row)
            row = re.sub(r" {2,}", " ", row)
        rows.append(row.rstrip())
    return rows


def kept_rows(rows: list[str], volatile=VOLATILE) -> list[str]:
    return [row for row in rows if not any(rule.search(row) for rule in volatile)]


# A running turn paints rows that come and go with time alone: Claude's spinner verb row and its
# tip row (which also shifts the rows below by one). Held-state comparisons drop them and the
# blank rows, so only what the turn did not paint has to be the same.
RUNNING_ROWS = (
    re.compile(r"^\s*[✶✻✳✢✽·*●]\s+\S+…"),
    re.compile(r"\bTip:"),
)


def screen_digest(text: str, volatile=VOLATILE, blank=(), native=False, running=False) -> dict:
    """A digest of the screen text without the rows that change with time alone.

    ``blank`` patterns (the "new release waiting" note) are cut out of their row instead of
    dropping it, so a row that also carries panel text still compares.
    """
    rows = screen_rows(text, blank, native)
    while rows and not rows[-1]:
        rows.pop()
    kept = kept_rows(rows, volatile)
    if running:
        kept = [row for row in kept_rows(kept, RUNNING_ROWS) if row.strip()]
    body = "\n".join(kept)
    return {"sha": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16], "rows": len(kept),
            "dropped": len(rows) - len(kept)}


def row_diff(before: list[str], after: list[str], limit: int = 8) -> list[str]:
    """The first rows that differ between two screens, for a failure row."""
    found = []
    for index in range(max(len(before), len(after))):
        one = before[index] if index < len(before) else ""
        two = after[index] if index < len(after) else ""
        if one != two:
            found.append(f"{index}: {one.strip()[:90]!r} -> {two.strip()[:90]!r}")
            if len(found) >= limit:
                break
    return found


def runtime_home(harness: str, env: dict) -> Path:
    """Where the install puts the ``hearting`` link for ``harness`` (fixture env)."""
    if harness == "claude":
        return Path(env["CLAUDE_CONFIG_DIR"])
    if harness == "codex":
        return Path(env["CODEX_HOME"])
    return Path(env["XDG_CONFIG_HOME"]) / "opencode"


def link_entry(harness: str, env: dict) -> Path:
    """The session shell entry reached through the install link, so the host path is the link."""
    return runtime_home(harness, env) / "hearting" / "utilities" / "session-shell" / "entry.py"


def upgrade_argv(harness: str, native: list[str], entry: Path) -> list[str]:
    return [sys.executable, str(entry), harness, *native]


def resume_native(harness: str, binary: str, sid: str, model: str) -> list[str]:
    """The harness's own command to reopen session ``sid`` (same flags as the first start)."""
    if harness == "claude":
        return [binary, "--model", model, "--resume", sid]
    if harness == "codex":
        return [binary, "--no-alt-screen", "resume", sid]
    return [binary, "--standalone", "-s", sid]


def inject_failure(host_text: str) -> str:
    """``host.cjs`` text whose successor (generation > 0) throws when the fixture asks."""
    if FAIL_ENV in host_text:
        return host_text
    if host_text.startswith("#!"):
        head, _, rest = host_text.partition("\n")
        return head + "\n" + FAIL_LINE + rest
    return FAIL_LINE + host_text


def release_copy(source: Path, dest: Path, *, label: str = "B", fail: bool = False) -> Path:
    """A copy of the tree being measured with a harmless marker file (a different release).

    The working tree is copied (tracked and untracked, not ignored, no ``.git``) so release B
    has the same content as release A; only the marker file tells them apart, which is what
    gives each its own bundle hash.
    """
    names = subprocess.run(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, check=True).stdout
    dest.mkdir(parents=True, exist_ok=True)
    for raw in names.split(b"\0"):
        if not raw:
            continue
        origin = source / os.fsdecode(raw)
        if not origin.exists():
            continue
        target = dest / os.fsdecode(raw)
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_symlink():
            if not target.is_symlink():
                target.symlink_to(os.readlink(origin))
        else:
            shutil.copy2(origin, target)
    shell = dest / "utilities" / "session-shell"
    (shell / MARKER_FILE).write_text(f"fixture release {label} marker\n", encoding="utf-8")
    if fail:
        host = shell / "host.cjs"
        host.write_text(inject_failure(host.read_text(encoding="utf-8")), encoding="utf-8")
    return dest


def pid_alive(pid) -> bool:
    return isinstance(pid, int) and pid > 1 and Path(f"/proc/{pid}").exists()


def bindings(env: dict, harness: str) -> list[dict]:
    """Live session-shell bindings of this fixture, newest first."""
    root = Path(env["XDG_STATE_HOME"]) / "agent-fleet" / "sessions" / harness
    found = []
    for path in sorted(root.glob("session-shell-*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True) if root.is_dir() else []:
        with contextlib.suppress(OSError, ValueError):
            value = json.loads(path.read_text(encoding="utf-8"))
            if pid_alive(value.get("host_pid")) and pid_alive(value.get("native_pid")):
                found.append(value)
    return found


def tmux_numbers(text: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"\s*(\d+),(\d+)\s*", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


COLUMNS = ("native_before", "native_after", "host_before", "host_after", "deferred",
           "waiting_note", "switched", "switched_after_release", "switched_after_next_prompt",
           "screen_same", "cursor_same", "key_reached",
           "l1_before", "l1_after", "l3_before", "l3_after", "scrollback_line", "fail_injection",
           "routes", "notice", "delivery_arrivals")


def render_table(rows: list[dict]) -> str:
    """The per-harness switch table (``results.md`` columns) from result rows."""
    lines = ["| harness | scenario | verdict | " + " | ".join(COLUMNS) + " |",
             "|---|---|---|" + "---|" * len(COLUMNS)]
    for row in rows:
        ev = (row.get("evidence") or {}).get("upgrade") or {}
        cells = []
        for column in COLUMNS:
            value = ev.get(column, "-")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            cells.append(str(value).replace("|", "/").replace("\n", " ")[:90])
        lines.append(f"| {row.get('harness')} | {row.get('scenario')} | {row.get('verdict')} | "
                     + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Live plumbing
# --------------------------------------------------------------------------

def _base(run):
    return sys.modules[type(run).__module__]


def _tmux(tui, *args):
    return tui._tmux(*args)


def snapshot(run, quiet: float = 3, timeout: float = 20) -> dict:
    """PIDs, screen digest, cursor and outer history of the tmux pane right now.

    A held state (a running step) keeps repainting a timer, so it waits only briefly to settle.
    """
    tui = run.tui
    tui.settle(quiet, timeout)
    screen = _tmux(tui, "capture-pane", "-p", "-t", "scenario").stdout or ""
    cursor = tmux_numbers(_tmux(tui, "display", "-p", "-t", "scenario",
                                "#{cursor_x},#{cursor_y}").stdout)
    history = _tmux(tui, "capture-pane", "-p", "-t", "scenario", "-S", "-", "-E", "-").stdout or ""
    size = (_tmux(tui, "display", "-p", "-t", "scenario", "#{history_size}").stdout or "").strip()
    found = bindings(run.home.env, run.harness)
    bound = found[0] if found else {}
    return {"native_pid": bound.get("native_pid"), "host_pid": bound.get("host_pid"),
            "digest": screen_digest(screen), "cursor": cursor,
            "history": screen_digest(history), "history_size": size}


def pane_key(run, *keys):
    run.tui.key(*keys)


def activation_argv(source_root: Path, harness: str, release: Path) -> list[str]:
    """The packaged activation of ``release`` (the real code that flips the ``hearting`` link)."""
    return ["sh", str(source_root / "tools/install/harness.sh"), "runtime", "activate",
            "--runtime", harness, "--mode", "packaged", "--source", str(release), "--json"]


def activate_release(run, release: Path, name: str) -> dict:
    """Packaged-activate ``release`` into the fixture's runtime home.

    A fixture ``harness.sh install`` is a source (linked) install: it links each item into
    the checkout and makes no ``<runtime_home>/hearting`` link, so only a packaged activation
    gives the link the session shell host watches.  Activation also writes the hooks and
    settings the oracles need.  The scheduler step stays off so no user-level unit is touched.
    """
    base = _base(run)
    env = dict(run.home.env, HARNESS_SCHEDULER_NO_ACTIVATE="1")
    log = Path(run.home.root) / f"activate-{name}.log"
    started = time.time()
    done = subprocess.run(activation_argv(Path(base.SOURCE), run.harness, release),
                          cwd=str(release), env=env, capture_output=True, text=True, timeout=900)
    log.write_text(done.stdout + "\n" + done.stderr, encoding="utf-8")
    link = runtime_home(run.harness, run.home.env) / "hearting"
    target = os.path.realpath(link) if link.exists() else None
    result = {"exit": done.returncode, "seconds": round(time.time() - started, 1),
              "log": str(log), "link_target": target}
    if done.returncode != 0:   # the fixture is removed afterwards; keep what failed
        result["log_tail"] = (done.stdout + done.stderr)[-900:]
    return result


def to_packaged(run) -> dict:
    """Replace the base harness's linked install with a packaged activation of release A."""
    base = _base(run)
    home = runtime_home(run.harness, run.home.env)
    # The fixture's own runtime home; the linked install is not a base for a packaged one.
    shutil.rmtree(home, ignore_errors=True)
    release_a = release_copy(Path(base.SOURCE), Path(run.home.root) / "release-a", label="A")
    result = activate_release(run, release_a, "a")
    if result["exit"] == 0:
        run.home.configure_model()
        if run.harness == "codex":
            run.evidence["hook_trust"] = run.home.trust_codex_hooks(run.binary)
    return result


def install_release_b(run, *, fail=False) -> dict:
    """"Install" the marked copy: packaged-activate release B into the same fixture HOME."""
    base = _base(run)
    copy = release_copy(Path(base.SOURCE), Path(run.home.root) / "release-b", label="B", fail=fail)
    return activate_release(run, copy, "b")


def host_status(run) -> dict:
    """The host's own readiness answer (state, empty, focus, held bytes) for a diagnosis row.

    Asking is the same control request the product's own tools make; it is only used after a
    switch failed to show, so it never changes a measurement.
    """
    base = _base(run)
    found = bindings(run.home.env, run.harness)
    if not found:
        return {"ok": False, "reason": "no live binding"}
    sys.path.insert(0, str(Path(base.SOURCE) / "utilities"))
    import session_shell  # noqa: E402
    old = {key: os.environ.get(key) for key in run.home.env if key in ("XDG_STATE_HOME", "HOME")}
    os.environ.update({key: run.home.env[key] for key in ("XDG_STATE_HOME", "HOME")})
    try:
        answer = session_shell.control(found[0], "status")
    except Exception as exc:  # a diagnosis must not fail the run
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    keep = ("ok", "reason", "state", "empty", "alive", "focused", "held_bytes", "projection_state")
    # The session the host has bound (`-` is the seat published with no selected session).
    return {"bound_sid": found[0].get("sid"), **{key: answer.get(key) for key in keep if key in answer}}


def probe(run, label: str) -> None:
    """Record what the host knows at a measurement point (diagnosis only, never a verdict input)."""
    run.evidence.setdefault("upgrade", {}).setdefault("host_probes", {})[label] = host_status(run)


def wait_host_change(run, old_host: int, timeout: float = SWITCH_WAIT):
    def moved():
        found = bindings(run.home.env, run.harness)
        return found[0] if found and found[0].get("host_pid") != old_host else None
    return wait_for(moved, timeout)


def key_reaches_native(run) -> bool:
    before = len(run.main_requests())
    typed = run.tui.type_text(KEY_TEXT)
    if not typed:
        return False
    return bool(wait_for(lambda: any(KEY_TEXT in r["text"] for r in run.main_requests()[before:]), 30))


def history_ops(run) -> dict:
    """The L1 operation (PageUp once, PageDown once) with the digest it shows, then closed."""
    tui = run.tui
    tui.settle(3, 20)
    tui.key("PageUp")
    tui.settle(2, 10)
    shown = screen_digest(_tmux(tui, "capture-pane", "-p", "-t", "scenario").stdout or "")
    tui.key("PageDown")
    tui.settle(2, 10)
    return shown


class NotProduced(Exception):
    """The state a variant needs could not be made on this harness."""


# --------------------------------------------------------------------------
# session-upgrade family
# --------------------------------------------------------------------------

class SessionUpgradeScenario(Scenario):
    name = "session-upgrade"
    summary = ("Idle session: install release B; the host hands over, the native PID stays, "
               "the screen and cursor are the same, and the next key reaches the native.")
    hold = False            # a held state must defer the switch until it is released
    fail = False
    scrollback = False
    DRAFT = ""
    next_turn_ok = False

    # -- model script ------------------------------------------------------
    def script(self, run):
        rows = int(os.environ.get("HEARTING_UPGRADE_ROWS", "50"))
        self.gate = threading.Event()

        def answer(record, earlier):
            text, kind = record["last_text"], record["last_kind"]
            if kind == "tool":
                return Reply("held step done")
            if KEY_TEXT in text:
                return Reply("key noted")
            if "SCENARIO-UPGRADE-LONG" in text:
                # The turn is held by the model's own slow reply (the gate), not by a tool step the
                # harness may move to the background and so end the turn (Codex does).
                return Reply("long turn answered", gate=self.gate)
            if "SCENARIO-UPGRADE-SCROLL" in text:
                return Reply("\n".join(f"scroll row {i:03d} SCENARIO-UPGRADE" for i in range(rows * 3)))
            if "SCENARIO-UPGRADE-ASK" in text:
                return Reply(text="I need one choice.", shell=native_question())
            return Reply(f"upgrade idle {record['main_index']}")
        return answer

    def cleanup(self, run):
        self.gate.set()     # no reply waits on the gate once the run is over

    # -- terminal ----------------------------------------------------------
    def prepare(self, run):
        enable_session_shell(run.home)
        run.evidence["activate_a"] = to_packaged(run)
        if run.evidence["activate_a"]["exit"] != 0:
            raise RuntimeError("release A activation failed: " + run.evidence["activate_a"]["log"])
        if self.fail:
            run.home.env[FAIL_ENV] = "1"

    def make_tui(self, run, root, argv):
        base = _base(run)
        native = base._native_argv(run.harness, run.binary)
        entry = link_entry(run.harness, run.home.env)
        run.evidence["link_entry"] = str(entry)
        return base.PrivateTui(root, run.home.env, upgrade_argv(run.harness, native, entry),
                               run.home.workspace)

    # -- the held state ----------------------------------------------------
    def hold_state(self, run):
        """Create the state that must defer the switch; return a note. May raise NotProduced."""
        return "-"

    def release_state(self, run):
        return None

    def first_turn(self, run) -> bool:
        text = "SCENARIO-UPGRADE-SCROLL fill" if self.scrollback else "SCENARIO-UPGRADE idle"
        run.prompt(text)
        if not run.wait_requests(1, 90):
            run.note("no first model request")
            return False
        run.wait_idle(6, 90)
        if self.scrollback:
            # The history view is native input: the host cannot prove an empty composer after
            # keys that were not submitted, so measure the view once, then submit a second
            # (identical) screenful so the input frontier is exact again before the install.
            run.evidence.setdefault("upgrade", {})["l1_before"] = history_ops(run)
            run.prompt(text)
            if not run.wait_requests(2, 90):
                run.note("no second scrollback request")
                return False
            run.wait_idle(6, 90)
        return True

    # -- the measurement ---------------------------------------------------
    def drive(self, run):
        ev = run.evidence.setdefault("upgrade", {})
        if not self.first_turn(run):
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found")
            return "inconclusive"
        probe(run, "after-first-turn")
        try:
            held_note = self.hold_state(run) if self.hold else "-"
        except NotProduced as reason:
            ev.update(deferred=f"{NOT_PRODUCED}: {reason}")
            run.note(f"{NOT_PRODUCED}: {reason}")
            return NOT_PRODUCED
        probe(run, "after-hold")
        quick = (1, 3) if self.hold else (3, 20)
        before = snapshot(run, *quick)
        before_text = run.tui.screen()
        before_quiet = screen_digest(before_text, blank=WAIT_NOTE, native=True, running=self.hold)
        ev.update(native_before=before["native_pid"], host_before=before["host_pid"])
        if not before["host_pid"] or not before["native_pid"]:
            run.note("no live session-shell binding before the install")
            return "inconclusive"
        if self.scrollback:
            ev["l3_before"] = {"digest": before["history"], "history_size": before["history_size"]}
        ev["install_b"] = install_release_b(run, fail=self.fail)
        if ev["install_b"]["exit"] != 0:
            run.note("release B activation failed")
            return "inconclusive"
        verdict = "pass"
        if self.hold:
            time.sleep(DEFER_WAIT)
            probe(run, "held")
            held = snapshot(run, *quick)
            shown = run.tui.screen()
            quiet = screen_digest(shown, blank=WAIT_NOTE, native=True, running=True)
            ev["waiting_note"] = "yes" if any(rule.search(row) for rule in WAIT_NOTE
                                              for row in screen_rows(shown)) else "no"
            same = (held["host_pid"] == before["host_pid"]
                    and held["native_pid"] == before["native_pid"]
                    and quiet["sha"] == before_quiet["sha"])
            ev["deferred"] = "yes" if same else "NO"
            if self.DRAFT:
                ev["draft_kept"] = self.DRAFT[:20] in shown
                if not ev["draft_kept"]:
                    verdict = "fail"
                    run.note("a draft byte was lost while the switch was held")
            if not same:
                ev["held_before"] = before
                ev["held_after"] = held
                ev["held_row_diff"] = row_diff(kept_rows(screen_rows(before_text, WAIT_NOTE, True)),
                                               kept_rows(screen_rows(shown, WAIT_NOTE, True)))
                verdict = "fail"
                run.note("the switch was not deferred while the state was held")
            self.release_state(run)
        moved = wait_host_change(run, before["host_pid"])
        released = bool(moved)
        if not moved:
            ev["host_status_after_wait"] = host_status(run)
            ev["screen_tail_after_wait"] = "\n".join(run.tui.screen().rstrip().splitlines()[-6:])
            ev["screen_after_wait"] = "\n".join(screen_rows(run.tui.screen(), native=True))
            if self.hold:
                # Information only: does one more submitted prompt let the held switch happen?
                count = len(run.main_requests())
                run.prompt("SCENARIO-UPGRADE follow-up")
                run.wait_requests(count + 1, 60)
                run.wait_idle(6, 90)
                moved_next = wait_host_change(run, before["host_pid"], 20)
                ev["switched_after_next_prompt"] = bool(moved_next)
                if self.next_turn_ok and moved_next:
                    moved = moved_next
        elif self.hold:
            ev["switched_after_next_prompt"] = "-"
        after = snapshot(run)
        ev.update(native_after=after["native_pid"], host_after=after["host_pid"])
        if self.fail:
            kept = (after["host_pid"] == before["host_pid"]
                    and after["native_pid"] == before["native_pid"] and not moved)
            ev["fail_injection"] = "old host kept" if kept else "FAILED: host changed"
            # What the person sees of the native is the same apart from the one waiting line the
            # old host paints (the host's own panel keeps counting time and is left out).
            final_text = run.tui.screen()
            ev["screen_same"] = (screen_digest(final_text, blank=WAIT_NOTE, native=True, running=self.hold)["sha"]
                                 == before_quiet["sha"])
            ev["cursor_same"] = after["cursor"] == before["cursor"]
            ev["stderr_on_screen"] = any(mark in final_text for mark in LOAD_ERROR_MARKS)
            if ev["stderr_on_screen"]:
                ev["screen_same"] = False
            if not ev["screen_same"] or not ev["cursor_same"]:
                ev["cursor"] = {"before": before["cursor"], "after": after["cursor"]}
                ev["screen_row_diff"] = row_diff(kept_rows(screen_rows(before_text, WAIT_NOTE, True)),
                                                 kept_rows(screen_rows(final_text, WAIT_NOTE, True)))
            ev["key_reached"] = key_reaches_native(run)
            return "pass" if kept and ev["screen_same"] and ev["key_reached"] and verdict == "pass" \
                else "fail"
        if self.hold:
            # "Switch only when the composer is provably empty": after an erased draft, an answered
            # form, history-scroll keys or a turn stopped without a stop callback that proof is the
            # next submitted turn; the report says which of the two moments it was.
            ev["switched_after_release"] = released
        else:
            ev["switched"] = bool(moved)
        if not moved:
            run.note("the host did not change after release B was installed"
                     + ("" if not self.next_turn_ok else " nor after the next submitted turn"))
            return "fail"
        ev["native_same"] = after["native_pid"] == before["native_pid"]
        if not self.hold:
            ev["screen_same"] = after["digest"] == before["digest"]
            ev["cursor_same"] = after["cursor"] == before["cursor"]
        if self.scrollback:
            ev["l1_after"] = history_ops(run)
            ev["l3_after"] = {"digest": after["history"], "history_size": after["history_size"]}
            ev["l1_same"] = ev["l1_before"] == ev["l1_after"]
            ev["l3_same"] = ev["l3_before"] == ev["l3_after"]
            ev["scrollback_line"] = "yes" if re.search(r"스크롤백 없이|without scrollback",
                                                      run.tui.screen()) else "no"
            ev["history_rule"] = VOLATILE_RULE
            ev["l1_operation"] = "PageUp once / PageDown once"
            ev["l2"] = "read by the synthetic seed round trip, not on the live TUI"
        ev["key_reached"] = key_reaches_native(run)
        ev["held_note"] = held_note
        ok = (ev["native_same"] and ev["key_reached"] and verdict == "pass"
              and (self.hold or (ev["screen_same"] and ev["cursor_same"]))
              and (not self.scrollback or (ev["l1_same"] and ev["l3_same"])))
        return "pass" if ok else "fail"


class ScrollbackScenario(SessionUpgradeScenario):
    name = "session-upgrade-scrollback"
    summary = "Three screens of output: outer history and the native's own history view survive."
    scrollback = True


class DraftScenario(SessionUpgradeScenario):
    name = "session-upgrade-draft"
    summary = "A draft is in the composer: the switch waits, the draft stays, then the switch happens."
    hold = True
    next_turn_ok = True     # an erased draft leaves the composer unproven: the next submitted turn settles it
    DRAFT = "SCENARIO-UPGRADE-DRAFT unsent"

    def hold_state(self, run):
        _tmux(run.tui, "send-keys", "-t", "scenario", "-l", self.DRAFT)
        if not wait_for(lambda: self.DRAFT[:20] in run.tui.screen(), 10):
            raise NotProduced("the draft never showed in the composer")
        return "draft typed"

    def release_state(self, run):
        run.tui.key("C-u")
        if not wait_for(lambda: self.DRAFT[:20] not in run.tui.screen(), 6):
            for _ in range(len(self.DRAFT)):
                run.tui.key("BSpace")


class TurnScenario(SessionUpgradeScenario):
    name = "session-upgrade-turn"
    summary = ("A turn is running (the model's reply is held): the switch waits, the turn keeps "
               "running, then the switch happens.")
    hold = True

    def hold_state(self, run):
        before = len(run.main_requests())
        run.tui.type_text("SCENARIO-UPGRADE-LONG run")
        if not run.wait_requests(before + 1, 60):
            raise NotProduced("the long turn never started")
        time.sleep(3)
        return "turn running: the model's reply is held"

    def release_state(self, run):
        # The model answers now; the turn ends by itself (a turn the person stops with Escape
        # fires no Stop hook on every harness, which is a different state).
        self.gate.set()
        run.wait_idle(6, 90)


class QuestionScenario(SessionUpgradeScenario):
    name = "session-upgrade-question"
    summary = "A native question is open: the switch waits until it is answered."
    hold = True
    next_turn_ok = True     # a form answered with Enter leaves the composer unproven too

    def hold_state(self, run):
        before = len(run.main_requests())
        run.tui.type_text("SCENARIO-UPGRADE-ASK please ask")
        if not run.wait_requests(before + 1, 60):
            raise NotProduced("no model request for the question")
        if not wait_for(lambda: OPTION_A in run.tui.screen(), 30):
            raise NotProduced("the question form never reached the screen")
        time.sleep(3)
        self.step_count = len(run.main_requests())
        rows = [row.strip()[-60:] for row in screen_rows(run.tui.screen()) if "question" in row.lower()]
        run.evidence.setdefault("upgrade", {})["question_rows"] = rows[:4]
        return "question form on screen"

    def release_state(self, run):
        # Pick the first option; each TUI may need one confirmation more.
        for keys in (["Enter"], ["Enter"], ["Tab", "Enter"]):
            run.tui.key(*keys)
            if wait_for(lambda: len(run.main_requests()) > self.step_count, 8):
                break
        run.wait_idle(5, 60)


ROUTE_TITLE = "ROUTE_PAGE"


def route_cache_value(old: dict | None, harness: str, sid: str, now: float) -> dict:
    """The panel cache with one fixture route row for this session (what a dispatched route shows).

    The row is the panel's own record shape (the PTY tests of the panel write the same one); no
    dispatch registry is touched, so only the display sees it.
    """
    value = dict(old) if isinstance(old, dict) else {
        "schema_version": 3, "items": [], "by_session": {}, "total": 0, "older": 0, "complete": True,
        "route_status": {}, "route_sessions": {}}
    key = harness + "\0" + sid
    row = {"id": "rt-0123456789abcdef", "title": ROUTE_TITLE, "state": "working", "worker_stage": "inline",
           "stages": [{"name": "inline", "st": "active"}], "harness": harness, "model": "fixture-model",
           "effort": "high", "ctx_pct": 40, "agents": 1,
           "visibility": {"codes": {"schema": "route_visibility_codes_v1", "shape": "direct",
                                    "next": {"kind": "closed"}, "verification": {"gate": "unproven"},
                                    "quoted": [["test", "EVIDENCE_%d bounded detail" % i] for i in range(8)]}}}
    value["observed_at"] = now
    value["route_panels"] = {**(value.get("route_panels") or {}), key: [row]}
    value["route_sessions"] = {**(value.get("route_sessions") or {}), key: now}
    return value


def seed_route_row(run, sid: str) -> bool:
    """Put a fixture route row in the panel cache of the fixture HOME; False if it cannot be written."""
    cache = Path(run.home.env["XDG_STATE_HOME"]) / "hearting" / "human-waits" / "projection.json"
    try:
        old = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = cache.with_name(cache.name + ".new")
        temporary.write_text(json.dumps(route_cache_value(old, run.harness, sid, time.time())), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, cache)
        return True
    except (OSError, ValueError):
        return False


class PageScenario(SessionUpgradeScenario):
    name = "session-upgrade-page"
    summary = "A panel read page is open: the switch waits until it is closed."
    hold = True
    LONG = "SCENARIO-UPGRADE-PAGE " + " ".join(f"line{i:02d}" for i in range(80))

    @staticmethod
    def panel_text(screen: str) -> list[tuple[int, int, str]]:
        """(row, left border column, text) of the host panel's own rows."""
        rows = []
        for index, line in enumerate(screen.splitlines()):
            plain = ANSI_RE.sub("", line).rstrip()
            cut = max(plain[:-1].rfind(char) for char in "│┌└") if plain[-1:] in ("│", "┐", "┘") else -1
            rows.append((index, cut, plain[cut:] if cut >= 0 else ""))
        return rows

    @staticmethod
    def heading(screen: str):
        """(row, 1-based column) inside the panel's ``╭─ route`` heading, which opens the route page."""
        for index, line in enumerate(screen.splitlines()):
            plain = ANSI_RE.sub("", line)
            at = plain.find("╭─ route")
            if at >= 0:
                cells = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in plain[:at])
                return index, cells + 3
        return None

    def openers(self, run):
        """Panel rows that open a read page: a route row, or a message that reads on (``›``)."""
        found = []
        for index, cut, text in self.panel_text(run.tui.screen()):
            inner = text.strip(" │")
            if cut >= 0 and (re.search(r"\brt-[0-9a-f]{6}", inner) or ROUTE_TITLE in inner or "⇄" in inner
                                or inner.rstrip().endswith("›")):
                found.append((index, cut, inner))
        return found

    def hold_state(self, run):
        ev = run.evidence.setdefault("upgrade", {})
        ev["route_row_seeded"] = seed_route_row(run, session_id(run) or "")
        wait_for(lambda: self.heading(run.tui.screen()) or self.openers(run), 20)
        if not (self.heading(run.tui.screen()) or self.openers(run)):
            # No route row showed: fall back to a long peer message, which an idle harness takes with
            # its next prompt and the panel then lists among the received messages.
            sent = send_peer(run, self.LONG, obligation=False)
            ev["page_message_ref"] = bool(sent.get("ref"))
            count = len(run.main_requests())
            run.prompt("SCENARIO-UPGRADE-PAGE-NEXT go on")
            run.wait_requests(count + 1, 60)
            run.wait_idle(5, 60)
            wait_for(lambda: self.openers(run), 30)
        ev["panel_rows"] = [f"{i}:{text.strip(' │')[:60]}" for i, c, text in
                            self.panel_text(run.tui.screen()) if c >= 0 and text.strip(" │┌└┐┘─")][:20]
        rows = self.openers(run)
        found = self.heading(run.tui.screen())
        if found:
            # The route section's heading opens the read page (its rows open in place).
            index, column = found
            inner = "route heading"
        elif rows:
            index, cut, inner = rows[0]
            column = cut + 4
        else:
            raise NotProduced("the panel shows no route row or long message to open as a read page")
        # An SGR mouse click (press, release) on the row, as a terminal sends it.
        _tmux(run.tui, "send-keys", "-t", "scenario", "-l",
              f"\x1b[<0;{column};{index + 1}M\x1b[<0;{column};{index + 1}m")
        time.sleep(2)
        shown = run.tui.screen()
        ev["page_rows"] = [f"{i}:{text.strip(' │')[:60]}" for i, c, text in
                           self.panel_text(shown) if c >= 0 and text.strip(" │┌└┐┘─")][:12]
        if "PgUp/PgDn" not in shown and not any(re.search(r"Back|‹|messages\s*·|route\s*·", text)
                                                for _i, _c, text in self.panel_text(shown)):
            raise NotProduced(f"the click at row {index} col {column} did not open a read page")
        return f"clicked panel row {index}: {inner[:40]}"

    def release_state(self, run):
        run.tui.key("Escape")


class DeliveryScenario(SessionUpgradeScenario):
    name = "session-upgrade-delivery"
    summary = "A peer delivery is in flight during a turn: the switch waits, the delivery arrives once."
    hold = True
    TOKEN = "SCENARIO-UPGRADE-PEER"

    def hold_state(self, run):
        TurnScenario.hold_state(self, run)
        sent = send_peer(run, f"[report] {self.TOKEN} worker finished", obligation=False)
        run.evidence["upgrade"]["delivery_ref"] = bool(sent.get("ref"))
        if not sent.get("ref"):
            raise NotProduced("no pending delivery reference was created")
        return "peer delivery pending"

    def release_state(self, run):
        TurnScenario.release_state(self, run)
        # The pending delivery follows the turn; it must reach the model, once.
        wait_for(lambda: any(self.TOKEN in r["text"] for r in run.main_requests()), 25)
        run.wait_idle(6, 40)

    def drive(self, run):
        verdict = super().drive(run)
        ev = run.evidence.setdefault("upgrade", {})
        # Codex hands a pending peer message to the model with the next submitted prompt (the key
        # check's), so give it until that turn is over before counting arrivals.
        wait_for(lambda: any(self.TOKEN in r["text"] for r in run.main_requests()), 40)
        run.wait_idle(6, 40)
        # Every later request carries the delivered text in its history, so the most any one
        # request holds is how many times it was delivered.
        arrivals = max([r["text"].count(self.TOKEN) for r in run.main_requests()] or [0])
        ev["delivery_arrivals"] = arrivals
        if verdict == "pass" and arrivals != 1:
            run.note(f"the peer delivery reached the model {arrivals} times, not once")
            return "fail"
        return verdict


class FailScenario(SessionUpgradeScenario):
    name = "session-upgrade-fail"
    summary = "The successor throws: the old host and native stay, the screen is the same, keys reach."
    fail = True


# --------------------------------------------------------------------------
# route-resume
# --------------------------------------------------------------------------

RECEIPT_CODE = r"""
import start_receipt as sr
from pathlib import Path
path = sr._directory(os.environ["AGENT_DISPATCH_JOBS"]) / (sr._key(args["harness"], args["sid"]) + ".json")
data = {}
try:
    data = json.loads(path.read_text())
except (OSError, ValueError):
    pass
receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else data
print(json.dumps({"route_id": receipt.get("route_id"), "saved_at": data.get("saved_at"), "mtime": path.stat().st_mtime if path.exists() else None}))
"""


class RouteResumeScenario(Scenario):
    name = "route-resume"
    summary = ("Reopen a session by its id: its open routes start by themselves, oldest first, "
               "and a notice left while it was closed arrives without a prompt.")
    ROUTES = (("closed", True), ("older", False), ("later", False))

    def prepare(self, run):
        from harness_behavior_cases_c import hide_herdr
        hide_herdr(run)
        enable_session_shell(run.home)
        run.evidence["activate_a"] = to_packaged(run)
        if run.evidence["activate_a"]["exit"] != 0:
            raise RuntimeError("release A activation failed: " + run.evidence["activate_a"]["log"])
        env, root = run.home.env, run.home.root
        tmp = root / "tmp"
        tmp.mkdir(exist_ok=True)
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(tmp)
        git = ["git", "-c", "user.name=fx", "-c", "user.email=fx@example.invalid"]
        self.repos = []
        for index, (label, _close) in enumerate(self.ROUTES, 1):
            repo = root / f"other{index}"
            subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
            (repo / "lib.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(git + ["-C", str(repo), "add", "lib.txt"], check=True, env=env)
            subprocess.run(git + ["-C", str(repo), "commit", "-qm", "base"], check=True, env=env)
            (repo / "lib.txt").write_text("two\n", encoding="utf-8")
            self.repos.append(repo)
        (root / "fx-evidence.txt").write_text("fixture evidence\n", encoding="utf-8")
        (root / "fx-summary.md").write_text("fixture summary: one-line change\n", encoding="utf-8")
        self.root, self.state = root, {"current": 0, "routes": {}}

    def make_tui(self, run, root, argv):
        base = _base(run)
        native = argv_native(base, run)
        entry = link_entry(run.harness, run.home.env)
        return base.PrivateTui(root, run.home.env, upgrade_argv(run.harness, native, entry),
                               run.home.workspace)

    def script(self, run):
        def commit(k):
            repo = shlex.quote(str(self.repos[k - 1]))
            return (f"git -C {repo} -c user.name=fx -c user.email=fx@example.invalid commit "
                    f"-qam 'fx change {k}' && echo FX-COMMIT-{k}-OK")

        def compose(k):
            cr = shlex.quote(str(Path(_base(run).SOURCE) / "utilities/capability-route.py")) \
                if run is not None else "capability-route.py"
            repo = shlex.quote(str(self.repos[k - 1]))
            return (f"python3 {cr} compose --shape direct --campaign-key other{k} --slug commit "
                    f"--cwd {repo} 2>&1; echo FX-COMPOSE-{k}-RC=$?")

        def answer(record, earlier):
            text, kind = record["last_text"], record["last_kind"]
            if kind != "tool":
                found = re.search(r"SCENARIO-RR (\d)", text)
                if found:
                    self.state["current"] = int(found.group(1))
                    if self.state["current"] > 1:
                        # The first refusal names the compose command; once the session has a
                        # route the gate lets later projects through, so route each one by hand.
                        return Reply(shell=compose(self.state["current"]))
                    return Reply(shell=commit(self.state["current"]))
                return Reply(f"route resume idle {record['main_index']}")
            k = self.state["current"]
            if not k:
                return Reply("nothing to do")
            if "has no route for" in text:
                plain = text.replace("\\n", "\n").replace('\\"', '"')
                lines = [ln.strip() for ln in plain.splitlines()
                         if "capability-route.py" in ln and " compose" in ln]
                if lines:
                    cmd = lines[0][lines[0].index("python3"):] if "python3" in lines[0] else lines[0]
                    return Reply(shell=cmd + f" 2>&1; echo FX-COMPOSE-{k}-RC=$?")
                return Reply("refused without a command")
            if f"FX-COMPOSE-{k}-RC=" in text:
                found = re.findall(r'"route_id": "(rt-[A-Za-z0-9_-]+)"', text) or \
                    re.findall(r"\b(rt-[0-9a-f]{8,})\b", text)
                if found:
                    self.state["routes"][k] = found[0]
                return Reply(shell=commit(k))
            if f"FX-COMMIT-{k}-OK" in text and self.ROUTES[k - 1][1] and \
                    f"FX-FINISH-{k}" not in text:
                route = self.state["routes"].get(k, "")
                cr = shlex.quote(str(Path(_base(run).SOURCE) / "utilities/capability-route.py"))
                arg = f"--route {shlex.quote(route)} " if route else ""
                return Reply(shell=(f"python3 {cr} finish {arg}--evidence "
                                    f"{shlex.quote(str(self.root / 'fx-evidence.txt'))} --summary-file "
                                    f"{shlex.quote(str(self.root / 'fx-summary.md'))} 2>&1; "
                                    f"echo FX-FINISH-{k}-RC=$?"))
            return Reply(f"route {k} done")
        return answer

    def cleanup(self, run):
        # The reopened pane's tmux server is not the one the base run holds.
        with contextlib.suppress(Exception):
            run.tui.stop()

    def _diagnostic(self, run, sid) -> dict:
        digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
        root = Path(run.home.env["HARNESS_STATE_ROOT"])
        for path in sorted(root.rglob(f"route-resume/{run.harness}-{digest}.json")):
            with contextlib.suppress(OSError, ValueError):
                return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def drive(self, run):
        base = _base(run)
        ev = run.evidence.setdefault("upgrade", {})
        for k in range(1, len(self.ROUTES) + 1):
            before = len(run.main_requests())
            run.tui.type_text(f"SCENARIO-RR {k} commit in project {k}")
            if not run.wait_requests(before + 1, 60):
                run.note(f"no model request for project {k}")
                return "inconclusive"
            run.wait_idle(8, 200)
        sid = session_id(run)
        if not sid:
            run.note("session id not found")
            return "inconclusive"
        routes = self.state["routes"]
        ev["created_routes"] = {str(k): v for k, v in routes.items()}
        if len(routes) < len(self.ROUTES):
            run.note("not every project got a route; the live conditions were not built")
            return "inconclusive"
        receipt_before = fixture_python(run, RECEIPT_CODE, {"harness": run.harness, "sid": sid})
        # Close the window: the native ends with the tmux server.
        run.tui.stop()
        time.sleep(3)
        ev["notice"] = fixture_python(run, NOTICE_CODE, {
            "harness": run.harness, "sid": sid, "key": "closed-" + sid[:8],
            "text": "SCENARIO-RR-NOTICE worker finished while the window was closed"})
        # Reopen the same session id through the install link, no hand command.
        native = resume_native(run.harness, run.binary, sid, base.FIXTURE_MODEL)
        entry = link_entry(run.harness, run.home.env)
        run.tui = base.PrivateTui(run.home.root, run.home.env,
                                  upgrade_argv(run.harness, native, entry), run.home.workspace)
        run.tui.start()
        wait_for(lambda: run.tui.alive() and run._ready(run.tui.screen()), 90)
        run._dismiss_startup(run.tui)
        wanted = [routes[2], routes[3]]
        diag = wait_for(lambda: (lambda d: d if len(d.get("routes") or []) >= 2 else None)(
            self._diagnostic(run, sid)), 120) or self._diagnostic(run, sid)
        results = diag.get("routes") or []
        ids = [r.get("route_id") for r in results]
        rcs = {r.get("route_id"): r.get("returncode") for r in results}
        ev["routes"] = {"order": ids, "rc": rcs}
        ev["closed_route_absent"] = routes[1] not in ids
        ev["open_in_order"] = ids[-2:] == wanted if len(ids) >= 2 else False
        receipt_after = fixture_python(run, RECEIPT_CODE, {"harness": run.harness, "sid": sid})
        ev["receipt"] = {"before": receipt_before.get("route_id"), "after": receipt_after.get("route_id")}
        ev["receipt_last"] = receipt_after.get("route_id") == routes[3]
        arrived = wait_for(lambda: any("SCENARIO-RR-NOTICE" in r["text"]
                                       for r in run.main_requests()), 60)
        ev["notice"] = "arrived without a prompt" if arrived else "not-delivered"
        ok = (ev["open_in_order"] and ev["closed_route_absent"]
              and all(rcs.get(r) == 0 for r in wanted) and ev["receipt_last"] and arrived)
        return "pass" if ok else "fail"


def argv_native(base, run) -> list[str]:
    return base._native_argv(run.harness, run.binary)


SCENARIOS = (SessionUpgradeScenario, ScrollbackScenario, DraftScenario, TurnScenario,
             QuestionScenario, PageScenario, DeliveryScenario, FailScenario,
             RouteResumeScenario)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", metavar="RESULTS_JSON",
                        help="print the per-harness switch table from a results.json")
    args = parser.parse_args(argv)
    if not args.table:
        parser.error("--table is required; run the scenarios with harness_behavior_scenarios.py")
    sys.stdout.write(render_table(json.loads(Path(args.table).read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
