#!/usr/bin/env python3
"""Behaviour scenarios: session-tidy clear -> continue -> completion, no typing.

Loaded by ``harness_behavior_scenarios.py`` (``SCENARIOS`` below).  Each runs
on the real TUI in the base harness's throwaway HOME with the loopback model.

The first prompt is typed.  The model answers with one shell call that
registers a synthetic route-bound depth-1 child (the ``harness_behavior_cases_a``
helper, as a frame row so the seat handover applies to it) and books
``session_tidy.py tidy`` with a marked card.  The turn ends; the tidy helper
clears the window.  From then on nobody types.  The oracle is what reaches
the model:

* ``first_untyped``: the new conversation sends a request with no typed prompt;
* ``card_on_first_turn``: that first request carries the card;
* ``continue_sent``: a new-conversation request is the booked continue text;
* ``completion_woke``: once the child completes, a request carrying the
  attempt id arrives with no typed prompt, and the model's next shell call runs.

Only when one of these misses does the scenario type one probe prompt (what
the person did on 2026-11-10) and record what that turn carried.

Surfaces: ``tidy-continue`` uses the private tmux socket with an interactive
host shell (herdr hidden).  The ``-herdr`` scenarios start a private Herdr
server (the default session) inside the fixture HOME and run the TUI in its
root pane; ``HEARTING_SCENARIO_HERDR_BIN`` or the caller's ``PATH`` names the
executable, and no other Herdr server is read or written.

``-now``: the child completes right after the clear is observed.
``-gone``: the cleared session's carrier has ended before the completion (as a
carrier at an open gate does once the seat handed the wait on).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
import harness_behavior_cases_a as ca  # noqa: E402
import harness_behavior_cases_c as cc  # noqa: E402

CARD_MARK = "TIDYC-CARD-MARK"
CARD_BODY = (f"원래 목표: {CARD_MARK} 창 비우기 뒤 이어가기 검증\n"
             "진행 중: 픽스처 세션 정리\n기다리는 결정: 없음\n다음 할 일: 새 대화에서 이어가기\n"
             "관련 세션: 없음")
CONTINUE_TEXT = "이어서해"
NEXT = "TIDYC-NEXT-ACTION"
# Resolved from the driver's own PATH before the fixture hides herdr.
HERDR_BIN = os.environ.get("HEARTING_SCENARIO_HERDR_BIN") or shutil.which("herdr") or ""

# The cases_a register helper writes a review row; a route-bound frame row is the
# shape whose recipient follows the seat handover after a clear.
ROUTED_PATCH = (
    '    meta += ["launch_claimed=1",',
    '    if "routed" in flags:\n'
    '        import hashlib\n'
    '        digest = hashlib.sha256(attempt.encode()).hexdigest()\n'
    '        meta = [m if m != "worker_type=review" else "worker_type=frame" for m in meta]\n'
    '        meta += ["route_id=rt-" + digest[:16], "route_hash=sha256:" + digest, '
    '"route_node=one-shot"]\n'
    '    meta += ["launch_claimed=1",')


def install_helper(run, attempt: str) -> None:
    ca._install_helper(run, attempt)
    helper = run.home.root / "hb_fx.py"
    text = helper.read_text(encoding="utf-8")
    if ROUTED_PATCH[0] not in text:
        raise RuntimeError("cases_a register helper changed; update ROUTED_PATCH")
    helper.write_text(text.replace(ROUTED_PATCH[0], ROUTED_PATCH[1], 1), encoding="utf-8")


class HerdrTui:
    """A private Herdr server (default session) in the fixture HOME; one root pane."""

    def __init__(self, root, env, argv, cwd):
        self.root, self.env, self.argv, self.cwd = Path(root), env, argv, cwd
        self.started, self.proc, self.pane, self.log = False, None, "", None

    def h(self, *args, timeout=30):
        return subprocess.run([HERDR_BIN, *args], env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def start(self):
        endpoint = Path(self.env["HERDR_SOCKET_PATH"])
        self.log = (self.root / "herdr.log").open("w")
        self.proc = subprocess.Popen([HERDR_BIN, "server"], env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=self.log)
        wait_for(lambda: endpoint.is_socket() or self.proc.poll() is not None, 10, 0.1)
        if not endpoint.is_socket():
            raise RuntimeError("private herdr unavailable")
        created = self.h("workspace", "create", "--cwd", str(self.cwd), "--label", "tidyc")
        value = json.loads(created.stdout or "{}").get("result", {}).get("root_pane")
        self.pane = value.get("pane_id") if isinstance(value, dict) else value
        if not self.pane:
            raise RuntimeError("no private pane: " + created.stdout + created.stderr)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(self.env.items()))
        launch = self.root / "launch.sh"
        launch.write_text(f"cd {shlex.quote(str(self.cwd))} && exec env -i {exports} "
                          'HERDR_ENV="$HERDR_ENV" HERDR_PANE_ID="$HERDR_PANE_ID" '
                          'HERDR_SOCKET_PATH="$HERDR_SOCKET_PATH" HERDR_BIN_PATH="$HERDR_BIN_PATH" '
                          + " ".join(shlex.quote(a) for a in self.argv) + "\n", encoding="utf-8")
        time.sleep(1.5)
        self.h("pane", "send-text", self.pane, f"sh {launch}")
        self.h("pane", "send-keys", self.pane, "enter")
        self.started = True

    def screen(self):
        r = self.h("pane", "read", self.pane, "--source", "recent-unwrapped", "--lines", "120",
                   "--format", "text")
        try:
            data = json.loads(r.stdout).get("result", {})
            return data.get("text") or data.get("content") or ""
        except ValueError:
            return r.stdout or ""

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def type_text(self, text, attempts=4):
        probe = text[:24]
        for _ in range(attempts):
            self.h("pane", "send-text", self.pane, text)
            if wait_for(lambda: probe in self.screen(), 6):
                time.sleep(0.6)
                self.h("pane", "send-keys", self.pane, "enter")
                return True
            self.h("pane", "send-keys", self.pane, "esc")
            time.sleep(1.5)
        return False

    def settle(self, quiet=3, timeout=45):
        deadline = time.time() + timeout
        last, since = None, time.time()
        while time.time() < deadline:
            current = self.screen()
            if current != last:
                last, since = current, time.time()
            elif time.time() - since >= quiet:
                return True
            time.sleep(0.5)
        return False

    def key(self, *keys):
        for key in keys:
            self.h("pane", "send-keys", self.pane, {"Enter": "enter", "Escape": "esc"}.get(key, key))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.log:
            self.log.close()
        self.started = False


def install_herdr_tui(run) -> None:
    if not HERDR_BIN:
        raise RuntimeError("herdr executable not found")
    env = run.home.env
    bindir = run.home.root / "herdr-bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "herdr").symlink_to(HERDR_BIN)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    env.pop("AGENT_HERDR_SESSION", None)   # the default server, as on a person's machine
    env["HERDR_SOCKET_PATH"] = str(Path(env["XDG_CONFIG_HOME"]) / "herdr" / "herdr.sock")
    module = cc._base_module(run)
    original = module.PrivateTui

    def factory(*args, **kwargs):
        module.PrivateTui = original     # one TUI per run
        return HerdrTui(run.home.root, *args[1:], **kwargs)
    module.PrivateTui = factory


DIAG = r"""
import importlib.util, json, sys
src, harness, path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path[:0] = [src + '/utilities', src + '/tools']
spec = importlib.util.spec_from_file_location('ps', src + '/utilities/peer-steward.py')
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)
req = json.load(open(path)); pane = (req.get('seat') or {}).get('pane')
out = {'pane': pane, 'new_session': req.get('new_session')}
try:
    out['look'] = ps._continue_look(pane, req)
    out['native'] = ps._continue_native_session(pane, harness, process_info=ps._retire_pane_info(pane))
except Exception as exc:
    out['error'] = repr(exc)
print(json.dumps(out, default=str)[:4000])
"""


def _is_memory_worker(record) -> bool:
    return "ops/session-tidy-memory" in record["text"]


class TidyContinue(Scenario):
    name = "tidy-continue"
    summary = ("Session tidy clears the window; with no typing the new conversation starts "
               "with the card and a later completion is handled (private tmux).")
    surface = "tmux"
    late = True
    end_old_arm = False

    def __init__(self):
        self.attempt = "att-tidyc-" + uuid.uuid4().hex[:20]

    def prepare(self, run):
        if self.surface == "herdr":
            install_herdr_tui(run)
        else:
            cc.hide_herdr(run)
            cc.install_tui(run, host_shell=True)
        install_helper(run, self.attempt)

    def script(self, run):
        def tidy():
            return ("python3 " + shlex.quote(str(run.home.root / "hb_fx.py")) + " register routed && "
                    'python3 "$AGENT_HOME/utilities/session_tidy.py" tidy --text '
                    + shlex.quote(CARD_BODY) + " 2>&1; echo FX-TIDY-RC=$?")

        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            done_next = any(NEXT in (r["reply"].get("shell") or "") for r in earlier)
            if kind == "tool":
                return Reply("Next action finished." if NEXT in last else "정리 요청 완료")
            if self.attempt in record["text"] and not done_next:
                return Reply(shell=f"echo {NEXT}-$((20+22))")
            if "SCENARIO-TIDYC-START" in last and not any(
                    "FX-TIDY-RC" in (r["reply"].get("shell") or "") for r in earlier):
                return Reply(shell=tidy())
            if CONTINUE_TEXT in last:
                return Reply("이어서 진행합니다 TIDYC-CONTINUED")
            return Reply(f"OK {record['main_index']}")
        return answer

    # -- observation -------------------------------------------------------
    def _booking_paths(self, run):
        return sorted((cc.state_home(run) / "hearting" / "session-tidy" / "clear").glob("*.json"))

    def _reservations(self, run):
        found = []
        for path in self._booking_paths(run):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found.append({"status": value.get("status"), "sid": value.get("sid"),
                          "new_session": value.get("new_session"),
                          "seat_kind": (value.get("seat") or {}).get("kind"),
                          "clear_claim": {k: (value.get("clear_claim") or {}).get(k)
                                          for k in ("state", "outcome")},
                          "continued": value.get("continued"), "reason": value.get("reason")})
        return found

    def diag(self, run):
        paths = self._booking_paths(run)
        if not paths:
            return None
        env = cc.tmux_env(run) if self.surface == "tmux" else run.home.env
        done = subprocess.run([sys.executable, "-B", "-c", DIAG, str(cc._source(run)), run.harness,
                               str(paths[-1])], env=env, text=True, capture_output=True, timeout=90)
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"stdout": done.stdout[-800:], "stderr": done.stderr[-1500:]}

    def _end_old_arm(self, run, old_sid):
        """End only this fixture's carrier process whose arm still names the cleared session."""
        arm = Path(run.home.env["AGENT_DISPATCH_JOBS"]).parent / "rewake-arms" / f"{self.attempt}.json"
        try:
            data = json.loads(arm.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"arm": None}
        out = {"arm_session": data.get("session_id"), "old": old_sid, "arms": data.get("arms"),
               "state": data.get("state"), "ended": False}
        if data.get("session_id") != old_sid or data.get("state") != "waiting":
            return out
        pid, start = int(data["holder"][0]), str(data["holder"][1])
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            mine = str(run.home.root).encode() in environ and stat[19] == start
        except OSError:
            mine = False
        if mine:
            os.kill(pid, signal.SIGTERM)
            out["ended"] = True
        return out

    def drive(self, run):
        ev = run.evidence
        t0 = time.time()
        run.prompt("SCENARIO-TIDYC-START 등록하고 세션을 정리해 주세요")
        run.wait_idle(8, 120)
        mains = run.main_requests()
        # The new conversation starts after the model's reply to the tidy tool result.
        split = next((i + 1 for i, r in enumerate(mains)
                      if r["last_kind"] == "tool" and "FX-TIDY-RC" in r["last_text"]), len(mains))
        tool = next((t for t in cc.tool_texts(run) if "FX-TIDY-RC" in t), "")
        ev["tidy_output"] = tool[-1500:]
        if "FX-TIDY-RC" not in tool:
            run.note("tidy shell call never reached the model")
            return "inconclusive"

        def cleared():
            return [r for r in self._reservations(run)
                    if r.get("new_session") or r.get("status") == "cleared"] or None
        manual = "clear=manual" in tool
        hit = None if manual else wait_for(cleared, 90, 0.3)
        ev["cleared_after_s"] = round(time.time() - t0, 1) if hit else None
        if not hit:
            ev["reservations_final"] = self._reservations(run)
            run.note("tidy answered clear=manual; nothing cleared without a person" if manual
                     else "the window was not cleared or the new session was not observed")
            return "gap" if manual else "fail"
        old_sid = hit[0].get("sid")

        def conversation():
            return [r for r in run.main_requests()[split:] if not _is_memory_worker(r)]
        time.sleep(3)
        ev["diag_after_clear"] = self.diag(run)
        completed_at = None
        if not self.late:
            completed_at = time.time()
            ev["complete"] = ca._helper(run, "complete")
        first = wait_for(lambda: conversation() or None, 150, 0.5)   # booking window is 120 s
        if first:
            req = first[0]
            ev["first_new_request"] = {"last_text": req["last_text"][-300:],
                                       "has_card": CARD_MARK in req["text"],
                                       "has_continue": CONTINUE_TEXT in req["last_text"],
                                       "has_attempt": self.attempt in req["text"]}
        else:
            ev["diag_after_window"] = self.diag(run)
        ev["first_untyped"] = bool(first)
        ev["card_on_first_turn"] = bool(first and CARD_MARK in first[0]["text"])
        ev["continue_sent"] = any(CONTINUE_TEXT in r["last_text"] for r in conversation())
        run.wait_idle(8, 60)
        if self.late:
            time.sleep(6)
            if self.end_old_arm:
                ev["old_arm_end"] = self._end_old_arm(run, old_sid)
                time.sleep(3)
            completed_at = time.time()
            ev["complete"] = ca._helper(run, "complete")
        got = wait_for(lambda: [r for r in conversation() if self.attempt in r["text"]] or None,
                       150, 1)
        run.wait_idle(8, 60)
        ev["completion_woke"] = bool(got)
        if got:
            ev["completion_seconds"] = round(got[0]["at"] - completed_at, 1)
        ev["next_action_ran"] = any(f"{NEXT}-42" in r["text"] for r in conversation())
        ev["probe_after_wait"] = ca._helper(run, "probe")
        ev["reservations_final"] = self._reservations(run)
        ok = bool(first) and ev["card_on_first_turn"] and ev["completion_woke"] \
            and ev["next_action_ran"]
        if not ok:
            count = len(run.main_requests())
            run.prompt("SCENARIO-TIDYC-PROBE 왜 이어서 안 하지?")
            run.wait_idle(8, 60)
            later = [r for r in run.main_requests()[count:] if not _is_memory_worker(r)]
            ev["typed_probe"] = {"requests": len(later),
                                 "card": any(CARD_MARK in r["text"] for r in later),
                                 "attempt": any(self.attempt in r["text"] for r in later)}
        yes = {True: "yes", False: "no"}
        run.note(f"first_untyped={yes[bool(first)]} continue={yes[ev['continue_sent']]} "
                 f"card_first={yes[ev['card_on_first_turn']]} "
                 f"completion={yes[ev['completion_woke']]} next={yes[ev['next_action_ran']]}")
        return "pass" if ok else "fail"


class TidyContinueHerdr(TidyContinue):
    name = "tidy-continue-herdr"
    summary = "tidy-continue in a private Herdr server (the completion lands after the continue)."
    surface = "herdr"


class TidyContinueHerdrNow(TidyContinueHerdr):
    name = "tidy-continue-herdr-now"
    summary = "tidy-continue-herdr with the completion right after the clear is observed."
    late = False


class TidyContinueHerdrGone(TidyContinueHerdr):
    name = "tidy-continue-herdr-gone"
    summary = "tidy-continue-herdr where the cleared session's carrier ended before the completion."
    end_old_arm = True


SCENARIOS = (TidyContinue, TidyContinueHerdr, TidyContinueHerdrNow, TidyContinueHerdrGone)
