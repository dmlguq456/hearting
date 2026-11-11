#!/usr/bin/env python3
"""Behaviour scenarios D: a failed shell command, and an interrupted turn.

Loaded by ``harness_behavior_scenarios.py`` (``SCENARIOS`` below), on the same
throwaway HOME, loopback model and private tmux socket as the other cases.

* ``failed-shell-hint`` — a Bash command fails on a stale Git ``index.lock``.
  The Git index-lock hint must reach the model with that tool result, and a
  Fleet wait seeded while the command runs must be gone when the model is
  asked next (the failure path clears it, not the later Stop or prompt).
* ``interrupt-change-record`` — the user interrupts a turn whose command has
  already changed a tracked file; the turn's automatic change record must
  still close with that change.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
from harness_behavior_cases_b import long_shell  # noqa: E402
from harness_behavior_cases_c import (  # noqa: E402
    WAIT_SOURCE, native_session_id, py, state_home)

LOCK_HINT = "Hearting Git index-lock diagnosis"


def _git_repo(run):
    """A committed ``tracked.txt`` in the fixture workspace (already ``git init``-ed)."""
    ws, env = run.home.workspace, run.home.env
    (ws / "tracked.txt").write_text("base\n", encoding="utf-8")
    for args in (("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "fixture"), ("add", "tracked.txt"),
                 ("commit", "-q", "-m", "base")):
        subprocess.run(["git", "-C", str(ws), *args], env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _seed_wait(run, sid: str) -> Path | None:
    done = py(run, (
        "from fleet import interaction; import sys\n"
        "print(interaction.set_wait(sys.argv[1], sys.argv[2], 'permission', sys.argv[3]))"),
        sid, run.harness, WAIT_SOURCE[run.harness])
    sidecar = state_home(run) / "agent-fleet" / "interactions" / run.harness / f"{sid}.json"
    return sidecar if done.stdout.strip() == "True" and sidecar.exists() else None


class FailedShellHint(Scenario):
    name = "failed-shell-hint"
    summary = ("A Bash command fails on a stale Git index.lock: the index-lock hint reaches "
               "the model with the tool result, and a Fleet wait set during the command is "
               "cleared by the failure path before the model is asked again.")

    def __init__(self):
        self.sid = None
        self.sidecar = None
        self.at_tool_result = {}

    def prepare(self, run):
        _git_repo(run)
        # A stale lock left behind by a crashed Git process.
        (run.home.workspace / ".git" / "index.lock").write_text("", encoding="utf-8")

    def script(self, run):
        def answer(record, earlier):
            ran = any(r["reply"]["shell"] for r in earlier)
            if (not ran and record["last_kind"] != "tool"
                    and "SCENARIO-LOCK go" in record["last_text"]):
                # The prompt hooks have run; seed the wait just before the command.
                self.sidecar = _seed_wait(run, self.sid) if self.sid else None
                return Reply(shell="git add tracked.txt")
            if record["last_kind"] == "tool" and not self.at_tool_result:
                # Observed when the model is asked again, i.e. after the tool hooks ran.
                self.at_tool_result = {
                    "lock_error_in_result": "index.lock" in record["last_text"],
                    "hint_in_request": LOCK_HINT in record["text"],
                    "wait_present": bool(self.sidecar and self.sidecar.exists()),
                }
            return Reply(f"lock scenario ok {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-LOCK hello")
        if not run.wait_requests(1, 90):
            run.note("TUI never asked the model")
            return "inconclusive"
        run.wait_idle(5, 60)
        sid = wait_for(lambda: native_session_id(run), 20)
        ev["session_id"] = sid
        if not sid:
            run.note("native session id not found")
            return "inconclusive"
        self.sid = sid
        before = len(run.main_requests())
        run.prompt("SCENARIO-LOCK go")
        if not run.wait_requests(before + 1, 30):
            run.note("the go prompt never reached the model")
            return "inconclusive"
        run.wait_idle(8, 90)
        ev["wait_seeded"] = bool(self.sidecar)
        ev["at_tool_result"] = self.at_tool_result
        ev["wait_present_after_turn"] = bool(self.sidecar and self.sidecar.exists())
        obs = self.at_tool_result
        if not obs or not obs.get("lock_error_in_result"):
            run.note("the failing git command's result never reached the model")
            return "inconclusive"
        run.note(f"hint={obs['hint_in_request']} wait_cleared_on_failure_path="
                 f"{ev['wait_seeded'] and not obs['wait_present']}")
        if obs["hint_in_request"] and ev["wait_seeded"] and not obs["wait_present"]:
            return "pass"
        return "fail"


class InterruptChangeRecord(Scenario):
    name = "interrupt-change-record"
    summary = ("The user interrupts a turn after its command changed a tracked file; the "
               "turn's automatic change record still closes with that change.")

    def prepare(self, run):
        _git_repo(run)

    def script(self, run):
        def answer(record, earlier):
            ran = any(r["reply"]["shell"] for r in earlier)
            if (not ran and record["last_kind"] != "tool"
                    and "SCENARIO-INTERRUPT go" in record["last_text"]):
                return Reply(shell=long_shell(
                    "echo interrupted-change >> tracked.txt; sleep 45", 60000))
            return Reply(f"interrupt scenario ok {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-INTERRUPT hello")
        if not run.wait_requests(1, 90):
            run.note("TUI never asked the model")
            return "inconclusive"
        run.wait_idle(5, 60)
        sid = wait_for(lambda: native_session_id(run), 20)
        ev["session_id"] = sid
        if not sid:
            run.note("native session id not found")
            return "inconclusive"
        state = state_home(run) / "hearting" / "turn-changes" / run.harness / f"{sid}.json"
        before = len(run.main_requests())
        run.prompt("SCENARIO-INTERRUPT go")
        if not run.wait_requests(before + 1, 30):
            run.note("the go prompt never reached the model")
            return "inconclusive"
        tracked = run.home.workspace / "tracked.txt"
        changed = wait_for(lambda: "interrupted-change" in tracked.read_text(encoding="utf-8"), 20)
        ev["file_changed_before_interrupt"] = bool(changed)
        if not changed:
            run.note("the command never changed the tracked file")
            return "inconclusive"
        time.sleep(2)
        interrupted_at = time.time()
        run.tui.key("Escape")
        if run.harness == "opencode":
            # The OpenCode 2 TUI answers the first Escape with "esc again to interrupt" and
            # interrupts only on a second one shortly after; the person presses it twice.
            time.sleep(0.4)
            run.tui.key("Escape")
        time.sleep(1.5)
        if run.harness == "codex" and "esc to interrupt" in run.tui.screen().lower():
            run.tui.key("Escape")

        def ended():
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(state.read_text(encoding="utf-8"))
                return (data.get("turn_ended_at") or 0) >= interrupted_at - 1 and data
            return None

        data = wait_for(ended, 40, 1)
        ev["turn_ended_after_interrupt"] = bool(data)
        reports = run.home.workspace / ".agent_reports"
        mentioned = []
        for path in [state, *(reports.rglob("*") if reports.is_dir() else [])]:
            with contextlib.suppress(OSError, UnicodeError):
                if path.is_file() and "tracked.txt" in path.read_text(encoding="utf-8"):
                    mentioned.append(str(path.relative_to(run.home.root)))
        ev["change_recorded_in"] = mentioned[:6]
        with contextlib.suppress(OSError, ValueError):
            ev["state_after"] = {k: v for k, v in json.loads(state.read_text("utf-8")).items()
                                 if k in ("turn_started_at", "turn_ended_at", "pending", "records")}
        run.note(f"turn_ended={bool(data)} recorded_in={len(mentioned)}")
        if data and mentioned:
            return "pass"
        return "gap" if not state.exists() or not data else "fail"


SCENARIOS = [FailedShellHint, InterruptChangeRecord]


class ExitWaitClear(Scenario):
    name = "exit-wait-clear"
    summary = ("The user exits an idle TUI with its own exit command; the session's Fleet "
               "wait is cleared once the TUI has exited.")

    def prepare(self, run):
        from harness_behavior_cases_c import hide_herdr, install_tui
        hide_herdr(run)
        install_tui(run, host_shell=True)

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-EXIT hello")
        if not run.wait_requests(1, 90):
            run.note("TUI never asked the model")
            return "inconclusive"
        run.wait_idle(5, 60)
        run.tui.settle(3, 30)
        sid = wait_for(lambda: native_session_id(run), 20)
        ev["session_id"] = sid
        sidecar = _seed_wait(run, sid) if sid else None
        ev["wait_seeded"] = bool(sidecar)
        if not sidecar:
            run.note("wait could not be seeded")
            return "inconclusive"
        if run.harness == "claude":
            run.tui._tmux("send-keys", "-t", "scenario", "-l", "/exit")
            time.sleep(0.8)
            run.tui.key("Enter")
        else:
            run.tui.key("C-d")
            time.sleep(1)
            if run.tui.alive():
                run.tui.key("C-d")
        exited = bool(wait_for(lambda: not run.tui.alive(), 30))
        ev["exited"] = exited
        if not exited:
            run.note("TUI did not exit")
            return "inconclusive"
        exited_at = time.time()
        cleared = bool(wait_for(lambda: not sidecar.exists(), 45, 0.5))
        ev["wait_cleared_after_exit_s"] = round(time.time() - exited_at, 1) if cleared else None
        run.note(f"wait cleared within 45 s of exit: {cleared}")
        return "pass" if cleared else "fail"


SCENARIOS.append(ExitWaitClear)
