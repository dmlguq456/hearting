#!/usr/bin/env python3
"""Behaviour scenarios: a manually invoked session tidy compacts the same session.

Loaded by ``harness_behavior_scenarios.py`` (``SCENARIOS`` below).  Each runs on
the real TUI in the base harness's throwaway HOME with the loopback model.

The person types the harness's own tidy invocation once (Claude and OpenCode
``/session-tidy``, Codex ``$session-tidy``).  The model answers with one shell
call that registers a synthetic route-bound depth-1 child (the
``harness_behavior_cases_a`` helper, as a frame row) and runs
``session_tidy.py tidy`` with a marked card, then ends its turn.  From then on
nobody types.  The oracle is what reaches the model and the native history:

* ``booked``: the tidy answered ``clear=scheduled`` (a same-session ``/compact``);
* ``compacted``: the booking's ``/compact`` claim was confirmed by the session's
  own compaction boundary (``status=cleared`` with ``clear_claim.command=compact``);
* ``same_session``: the booking continues its own SID and the harness's newest
  native session is still that SID, so its Fleet number cannot change;
* ``continue_untyped`` / ``card_on_continue``: an untyped request carries the
  booked continue text and the card;
* ``completion_woke`` / ``next_action_ran``: the child's completion reaches the
  same session without typing and the model's next shell call runs.

``tidy_turn_requests`` counts the main-session requests of the tidy turn (the
prompt up to the reply after the tool result); ``compaction_requests`` counts
the requests the harness itself made for the booked ``/compact`` (between that
reply and the continue).  Memory-worker requests (a separate headless session)
are counted apart.  Only when the oracle misses does it type one probe prompt.

Surfaces: ``tidy-compact`` uses the private tmux socket with an interactive host
shell (herdr hidden).  ``-herdr`` starts a private Herdr server (default
session) inside the fixture HOME and runs the TUI in its root pane;
``HEARTING_SCENARIO_HERDR_BIN`` or the caller's ``PATH`` names the executable,
and no other Herdr server is read or written.  The driver's ``--session-shell``
runs each TUI through Hearting's session shell.

``-between``: the child completes right after the tidy turn, before the booked
``/compact`` runs (a completion that arrives while the tidy is pending).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
import harness_behavior_cases_a as ca  # noqa: E402
import harness_behavior_cases_c as cc  # noqa: E402

CARD_MARK = "TIDYK-CARD-MARK"
CARD_BODY = (f"원래 목표: {CARD_MARK} 같은 세션 압축 뒤 이어가기 검증\n"
             "진행 중: 픽스처 세션 정리\n기다리는 결정: 없음\n다음 할 일: 같은 세션에서 이어가기\n"
             "관련 세션: 없음")
CONTINUE_TEXT = "이어서해"
NEXT = "TIDYK-NEXT-ACTION"
START = "SCENARIO-TIDYK-START"
# The person's own tidy invocation in each harness.
INVOKE = {"claude": "/session-tidy", "codex": "$session-tidy", "opencode": "/session-tidy"}
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



def _is_memory_worker(record) -> bool:
    """The detached memory worker is a separate headless session; leave it out."""
    return "session-tidy-memory" in record["text"] or "Portable Worker Kernel" in record["text"]


SUMMARY_TAG = "<analysis>fixture</analysis>"


# The compaction prompt each harness sends for its own /compact (measured on Claude 2.1.322,
# Codex 0.163.0 and OpenCode 2.0.32).
COMPACTION_MARKS = ("<summary> block", "seamlessly continue the work",
                    "Return only the structured summary")


def _is_compaction(record) -> bool:
    return any(mark in record["last_text"] for mark in COMPACTION_MARKS)


class TidyCompact(Scenario):
    name = "tidy-compact"
    summary = ("A manually invoked tidy compacts the same session; with no typing it "
               "continues with the card and handles a later completion (private tmux).")
    surface = "tmux"
    between = False

    def __init__(self):
        self.attempt = "att-tidyk-" + uuid.uuid4().hex[:20]

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
            tidied = any("FX-TIDY-RC" in (r["reply"].get("shell") or "") for r in earlier)
            if tidied and CONTINUE_TEXT not in last and _is_compaction(record) \
                    and not _is_memory_worker(record):
                # The harness's own compaction request: answer it the way a model would.
                return Reply(f"{SUMMARY_TAG}\n<summary>TIDYK-SUMMARY "
                             f"{record['main_index']}</summary>")
            if kind == "tool":
                return Reply("Next action finished." if NEXT in last else "정리 요청 완료")
            if self.attempt in record["text"] and tidied and not done_next \
                    and CONTINUE_TEXT not in last:
                return Reply(shell=f"echo {NEXT}-$((20+22))")
            if START in record["text"] and not tidied:
                return Reply(shell=tidy())
            if CONTINUE_TEXT in last:
                if self.attempt in record["text"] and not done_next and self.between:
                    return Reply(shell=f"echo {NEXT}-$((20+22))")
                return Reply("이어서 진행합니다 TIDYK-CONTINUED")
            # Anything else after the tidy (the harness's own compaction request) gets a summary.
            return Reply(f"TIDYK-SUMMARY {record['main_index']}")
        return answer

    def _booking(self, run):
        paths = sorted((cc.state_home(run) / "hearting" / "session-tidy" / "clear").glob("*.json"))
        for path in reversed(paths):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("origin") in (None, "", "manual"):
                return value
        return None

    @staticmethod
    def _brief(value):
        if not value:
            return None
        return {"status": value.get("status"), "sid": value.get("sid"),
                "new_session": value.get("new_session"), "reason": value.get("reason"),
                "seat_kind": (value.get("seat") or {}).get("kind"),
                "clear_command": value.get("clear_command"),
                "snapshot_state": value.get("snapshot_state"),
                "clear_claim": {k: (value.get("clear_claim") or {}).get(k)
                                for k in ("command", "state", "outcome")},
                "continued": {k: (value.get("continued") or {}).get(k)
                              for k in ("state", "reason", "kind")}}

    def drive(self, run):
        ev = run.evidence
        before = len(run.main_requests())
        run.prompt(f"{INVOKE[run.harness]} {START} 등록하고 세션을 정리해 주세요")
        run.wait_idle(8, 120)
        mains = run.main_requests()
        split = next((i + 1 for i, r in enumerate(mains)
                      if r["last_kind"] == "tool" and "FX-TIDY-RC" in r["last_text"]), None)
        tool = next((t for t in cc.tool_texts(run) if "FX-TIDY-RC" in t), "")
        ev["tidy_output"] = tool[-1500:]
        if split is None:
            run.note("tidy shell call never reached the model")
            return "inconclusive"
        turn = [r for r in mains[before:split] if not _is_memory_worker(r)]
        ev["tidy_turn_requests"] = len(turn)
        ev["tidy_turn_kinds"] = [r["last_kind"] for r in turn]
        ev["invocation_seen"] = any(START in r["text"] for r in turn[:1])
        sid = cc.native_session_id(run)
        ev["sid_at_tidy"] = sid
        ev["booked"] = "clear=scheduled" in tool
        if not ev["booked"]:
            ev["booking"] = self._brief(self._booking(run))
            word = next((w for w in tool.split() if w.startswith("clear=")), "-")
            if "clear=manual" not in tool:
                run.note("tidy did not book the same-session compaction: " + word)
                return "fail"
            return self._manual(run, ev, split, sid, word)
        completed_at = None
        if self.between:
            completed_at = time.time()
            ev["complete"] = ca._helper(run, "complete")

        def conversation():
            return [r for r in run.main_requests()[split:] if not _is_memory_worker(r)]

        def settled():
            value = self._booking(run)
            cont = (value or {}).get("continued") or {}
            if value and value.get("status") not in ("reserved",) and (
                    value.get("status") != "cleared" or cont.get("state") not in (None, "pending", "sending")):
                return value
            return None
        final = wait_for(settled, 300, 1)
        ev["booking"] = self._brief(final or self._booking(run))
        booking = ev["booking"] or {}
        ev["compacted"] = (booking.get("status") == "cleared"
                           and (booking.get("clear_claim") or {}).get("command") == "compact")
        ev["same_session_booking"] = booking.get("new_session") == booking.get("sid") == sid
        resumed = [r for r in conversation() if not _is_compaction(r)
                   and SUMMARY_TAG not in str((r.get("reply") or {}).get("text") or "")]
        first = resumed[0] if resumed else None
        ev["first_untyped_after_compaction"] = None if first is None else {
            "last_text": first["last_text"][-240:], "has_card": CARD_MARK in first["text"],
            "has_continue": CONTINUE_TEXT in first["last_text"],
            "has_attempt": self.attempt in first["text"]}
        ev["resumed_untyped"] = bool(first)
        ev["card_on_resume"] = bool(first and CARD_MARK in first["text"])
        ev["continue_sent"] = any(CONTINUE_TEXT in r["last_text"] for r in conversation())
        cont_state = (booking.get("continued") or {})
        # One resumption: the booked continue, or another automatic turn that took the card first
        # (then the continue is skipped quietly as card-taken).
        ev["one_resumption"] = (ev["continue_sent"] and cont_state.get("state") == "sent") or (
            not ev["continue_sent"] and cont_state.get("reason") in ("card-taken", "not-idle-working"))
        ev["compaction_requests"] = sum(1 for r in run.fixture.requests
                                        if SUMMARY_TAG in str((r.get("reply") or {}).get("text") or ""))
        run.wait_idle(8, 60)
        if not self.between:
            time.sleep(4)
            completed_at = time.time()
            ev["complete"] = ca._helper(run, "complete")
        got = wait_for(lambda: [r for r in conversation() if self.attempt in r["text"]
                                and r.get("at", 0) >= (completed_at or 0) - 1] or None, 150, 1)
        run.wait_idle(8, 60)
        ev["completion_woke"] = bool(got)
        if got and completed_at:
            ev["completion_seconds"] = round(got[0]["at"] - completed_at, 1)
        ev["next_action_ran"] = any(f"{NEXT}-42" in r["text"] for r in conversation())
        ev["sid_at_end"] = cc.native_session_id(run)
        ev["same_session"] = bool(sid) and ev["sid_at_end"] == sid and ev["same_session_booking"]
        ev["memory_worker_requests"] = sum(1 for r in run.fixture.requests if _is_memory_worker(r))
        ev["side_requests"] = sum(1 for r in run.fixture.requests
                                  if not r["main"] and not _is_memory_worker(r))
        ev["probe_after_wait"] = ca._helper(run, "probe")
        ok = (ev["compacted"] and ev["same_session"] and ev["resumed_untyped"]
              and ev["card_on_resume"] and ev["one_resumption"]
              and ev["completion_woke"] and ev["next_action_ran"])
        if not ok:
            count = len(run.main_requests())
            run.prompt("SCENARIO-TIDYK-PROBE 왜 이어서 안 하지?")
            run.wait_idle(8, 60)
            later = [r for r in run.main_requests()[count:] if not _is_memory_worker(r)]
            ev["typed_probe"] = {"requests": len(later),
                                 "card": any(CARD_MARK in r["text"] for r in later),
                                 "attempt": any(self.attempt in r["text"] for r in later)}
        yes = {True: "yes", False: "no"}
        run.note(f"compacted={yes[bool(ev['compacted'])]} same_sid={yes[bool(ev['same_session'])]} "
                 f"resumed={yes[ev['resumed_untyped']]} card={yes[ev['card_on_resume']]} "
                 f"continue={yes[ev['continue_sent']]} one={yes[bool(ev['one_resumption'])]} "
                 f"completion={yes[ev['completion_woke']]} next={yes[ev['next_action_ran']]} "
                 f"tidy_turn={ev['tidy_turn_requests']} compaction={ev['compaction_requests']}")
        return "pass" if ok else "fail"


    def _manual(self, run, ev, split, sid, word):
        """No provable pane seat: the person types the printed /compact, then one prompt."""
        run.note(f"tidy answered {word}; typed /compact as the person would")
        run.tui.key("Escape")
        time.sleep(0.5)
        count = len(run.fixture.requests)
        run.tui.type_text("/compact")
        wait_for(lambda: any(SUMMARY_TAG in str((r.get("reply") or {}).get("text") or "")
                             for r in run.fixture.requests[count:]), 90, 1)
        run.wait_idle(8, 90)
        ev["compaction_requests"] = sum(1 for r in run.fixture.requests[count:]
                                        if SUMMARY_TAG in str((r.get("reply") or {}).get("text") or ""))
        mark = len(run.main_requests())
        run.prompt("SCENARIO-TIDYK-AFTER 이어서 해 주세요")
        run.wait_idle(8, 90)
        later = [r for r in run.main_requests()[mark:] if not _is_memory_worker(r)]
        ev["card_after_manual_compact"] = any(CARD_MARK in r["text"] for r in later)
        ev["sid_at_end"] = cc.native_session_id(run)
        ev["same_session"] = bool(sid) and ev["sid_at_end"] == sid
        run.note(f"manual /compact: compactions={ev['compaction_requests']} "
                 f"card_next_turn={'yes' if ev['card_after_manual_compact'] else 'no'} "
                 f"same_sid={'yes' if ev['same_session'] else 'no'} tidy_turn={ev['tidy_turn_requests']}")
        return "gap"


class TidyCompactBetween(TidyCompact):
    name = "tidy-compact-between"
    summary = "tidy-compact with the child's completion arriving before the booked /compact."
    between = True


class TidyCompactHerdr(TidyCompact):
    name = "tidy-compact-herdr"
    summary = "tidy-compact in a private Herdr server (completion after the continue)."
    surface = "herdr"


class TidyCompactHerdrBetween(TidyCompactHerdr):
    name = "tidy-compact-herdr-between"
    summary = "tidy-compact-herdr with the completion arriving before the booked /compact."
    between = True


SCENARIOS = [TidyCompact, TidyCompactBetween, TidyCompactHerdr, TidyCompactHerdrBetween]
