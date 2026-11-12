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
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import (ANSI_RE, Reply, Scenario,  # noqa: E402
                                        enable_session_shell, wait_for)
from harness_behavior_cases_b import (fixture_python, long_shell, send_peer,  # noqa: E402
                                      session_id)
from harness_behavior_cases_d import NOTICE_CODE  # noqa: E402

KEY_TEXT = "SCENARIO-UPGRADE-KEY"
SWITCH_WAIT = 30          # seconds the new host may take to appear
DEFER_WAIT = 12           # seconds a held state is watched for a (wrong) switch
MARKER_FILE = "upgrade-fixture-release.txt"
FAIL_ENV = "HEARTING_UPGRADE_FIXTURE_FAIL"
FAIL_LINE = ("if(process.env.%s==='1'&&Number(process.env.HEARTING_SHELL_GENERATION||0)>0)"
             "throw new Error('fixture: successor refuses');\n" % FAIL_ENV)
NOT_PRODUCED = "not-produced"
QUESTION = "[답변 필요] SCENARIO-UPGRADE-Q 하나만 정해 주세요."

# Lines that change with time, not with the session: spinners, clocks, elapsed counters.
VOLATILE = (
    re.compile(r"[⠀-⣿✻✶✳✢✽·●◐◓◑◒]\s*\S.*(?:\d+s\b|esc to interrupt|interrupt)", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    re.compile(r"\(\s*\d+(?:\.\d+)?\s*[smh]\b"),
    re.compile(r"\b\d+(?:\.\d+)?s\s*[·•|]"),
)
VOLATILE_RULE = ("drop rows with a spinner glyph plus an elapsed/interrupt hint, a clock "
                 "(h:mm[:ss]), '(12s' elapsed, or '12s ·'; trailing blanks and ANSI are removed")


# --------------------------------------------------------------------------
# Pure helpers (offline-testable)
# --------------------------------------------------------------------------

def screen_digest(text: str, volatile=VOLATILE) -> dict:
    """A digest of the screen text without the rows that change with time alone."""
    rows = [ANSI_RE.sub("", line).rstrip() for line in (text or "").splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    kept = [row for row in rows if not any(rule.search(row) for rule in volatile)]
    body = "\n".join(kept)
    return {"sha": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16], "rows": len(kept),
            "dropped": len(rows) - len(kept)}


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


def release_copy(source: Path, dest: Path, *, fail: bool = False) -> Path:
    """A copy of the tree being measured with a harmless marker file (a different release).

    The working tree is copied (tracked and untracked, not ignored) so release B has the
    same content as the release A that was installed from ``source``.
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
    (shell / MARKER_FILE).write_text("fixture release B marker\n", encoding="utf-8")
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
           "switched", "switched_after_release", "screen_same", "cursor_same", "key_reached",
           "l1_before", "l1_after", "l3_before", "l3_after", "scrollback_line", "fail_injection",
           "routes", "notice")


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


def snapshot(run) -> dict:
    """PIDs, screen digest, cursor and outer history of the tmux pane right now."""
    tui = run.tui
    tui.settle(3, 20)
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


def install_release_b(run, *, fail=False) -> dict:
    """Install the marked copy of the measured tree into the same fixture HOME."""
    base = _base(run)
    runtime = Path(run.home.root) / "release-b"
    copy = release_copy(Path(base.SOURCE), runtime, fail=fail)
    env = dict(run.home.env, AGENT_HOME=str(copy), GIT_CONFIG_VALUE_0=str(copy))
    for step in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=fx", "-c", "user.email=fx@example.invalid", "commit", "-qm", "b"]):
        subprocess.run(["git", "-C", str(copy), *step], env=env, capture_output=True)
    log = Path(run.home.root) / "install-b.log"
    started = time.time()
    with log.open("w", encoding="utf-8") as handle:
        done = subprocess.run(["bash", str(copy / "tools/install/harness.sh"), "install",
                               run.harness], cwd=str(copy), env=env, stdout=handle,
                              stderr=subprocess.STDOUT, timeout=900)
    return {"exit": done.returncode, "seconds": round(time.time() - started, 1), "log": str(log)}


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

    # -- model script ------------------------------------------------------
    def script(self, run):
        rows = int(os.environ.get("HEARTING_UPGRADE_ROWS", "50"))

        def answer(record, earlier):
            text, kind = record["last_text"], record["last_kind"]
            if kind == "tool":
                return Reply("held step done")
            if KEY_TEXT in text:
                return Reply("key noted")
            if "SCENARIO-UPGRADE-LONG" in text:
                return Reply(shell=long_shell("sleep 240; echo SCENARIO-UPGRADE-LONG-DONE", 240000))
            if "SCENARIO-UPGRADE-SCROLL" in text:
                return Reply("\n".join(f"scroll row {i:03d} SCENARIO-UPGRADE" for i in range(rows * 3)))
            if "SCENARIO-UPGRADE-ASK" in text:
                return Reply("확인했습니다.\n\n" + QUESTION)
            return Reply(f"upgrade idle {record['main_index']}")
        return answer

    # -- terminal ----------------------------------------------------------
    def prepare(self, run):
        enable_session_shell(run.home)
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
        try:
            held_note = self.hold_state(run) if self.hold else "-"
        except NotProduced as reason:
            ev.update(deferred=f"{NOT_PRODUCED}: {reason}")
            run.note(f"{NOT_PRODUCED}: {reason}")
            return NOT_PRODUCED
        before = snapshot(run)
        ev.update(native_before=before["native_pid"], host_before=before["host_pid"])
        if not before["host_pid"] or not before["native_pid"]:
            run.note("no live session-shell binding before the install")
            return "inconclusive"
        if self.scrollback:
            ev["l1_before"] = history_ops(run)
            ev["l3_before"] = {"digest": before["history"], "history_size": before["history_size"]}
        ev["install_b"] = install_release_b(run, fail=self.fail)
        if ev["install_b"]["exit"] != 0:
            run.note("release B install failed")
            return "inconclusive"
        verdict = "pass"
        if self.hold:
            time.sleep(DEFER_WAIT)
            held = snapshot(run)
            same = (held["host_pid"] == before["host_pid"]
                    and held["native_pid"] == before["native_pid"]
                    and held["digest"] == before["digest"])
            ev["deferred"] = "yes" if same else "NO"
            if not same:
                verdict = "fail"
                run.note("the switch was not deferred while the state was held")
            self.release_state(run)
        moved = wait_host_change(run, before["host_pid"])
        after = snapshot(run)
        ev.update(native_after=after["native_pid"], host_after=after["host_pid"])
        if self.fail:
            kept = (after["host_pid"] == before["host_pid"]
                    and after["native_pid"] == before["native_pid"] and not moved)
            ev["fail_injection"] = "old host kept" if kept else "FAILED: host changed"
            ev["screen_same"] = after["digest"] == before["digest"]
            ev["cursor_same"] = after["cursor"] == before["cursor"]
            ev["key_reached"] = key_reaches_native(run)
            return "pass" if kept and ev["screen_same"] and ev["key_reached"] and verdict == "pass" \
                else "fail"
        ev["switched_after_release" if self.hold else "switched"] = bool(moved)
        if not moved:
            run.note("the host did not change after release B was installed")
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
    summary = "A turn is running: the switch waits, the turn keeps running, then the switch happens."
    hold = True

    def hold_state(self, run):
        before = len(run.main_requests())
        run.tui.type_text("SCENARIO-UPGRADE-LONG run")
        if not run.wait_requests(before + 1, 60):
            raise NotProduced("the long turn never started")
        time.sleep(3)
        return "long shell step running"

    def release_state(self, run):
        for _ in range(3):
            run.tui.key("Escape")
            time.sleep(1)


class QuestionScenario(SessionUpgradeScenario):
    name = "session-upgrade-question"
    summary = "An answer question is open: the switch waits until it is answered."
    hold = True

    def hold_state(self, run):
        before = len(run.main_requests())
        run.tui.type_text("SCENARIO-UPGRADE-ASK please ask")
        if not run.wait_requests(before + 1, 60):
            raise NotProduced("no model request for the question")
        run.wait_idle(4, 60)
        if not wait_for(lambda: "SCENARIO-UPGRADE-Q" in run.tui.screen(), 20):
            raise NotProduced("the question never reached the screen")
        return "question on screen"

    def release_state(self, run):
        run.tui.type_text("answer alpha")
        run.wait_idle(4, 60)


class PageScenario(SessionUpgradeScenario):
    name = "session-upgrade-page"
    summary = "A panel read page is open: the switch waits until it is closed."
    hold = True

    def hold_state(self, run):
        rows = [(i, line) for i, line in enumerate(run.tui.screen().splitlines())
                if re.search(r"\broute\b", line, re.I)]
        if not rows:
            raise NotProduced("the panel shows no route row to open (needs an open route)")
        index, line = rows[-1]
        column = line.lower().index("route") + 1
        # An SGR mouse click on the row; the host reads it from stdin like a terminal would.
        _tmux(run.tui, "send-keys", "-t", "scenario", "-l",
              f"\x1b[<0;{column};{index + 1}M\x1b[<0;{column};{index + 1}m")
        time.sleep(1.5)
        return f"clicked panel row {index}"

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
print(json.dumps({"route_id": data.get("route_id"), "mtime": path.stat().st_mtime if path.exists() else None}))
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

        def answer(record, earlier):
            text, kind = record["last_text"], record["last_kind"]
            if kind != "tool":
                found = re.search(r"SCENARIO-RR (\d)", text)
                if found:
                    self.state["current"] = int(found.group(1))
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
