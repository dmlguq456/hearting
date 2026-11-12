#!/usr/bin/env python3
"""Idle-wake scenarios for ``harness_behavior_scenarios.py`` (BC-IDLE-WAKE-MISSED-1112).

A supervising session starts registered work, ends its turn and rests. The work
then finishes. Each scenario asks whether the resting session is woken and takes
its next action with no typed prompt, and how long that took. The plain
``completion-receipt`` scenario (``harness_behavior_cases_a``) is the baseline:

``idle-wake-frame``
    The registered child is a frame leg (``worker_type=frame``), the kind a
    route's direction step waits for.

``idle-wake-reinstall``
    Hearting is installed again from the same source while the session rests,
    before the child finishes (an update that leaves the running session alone).

``idle-wake-restart``
    While the session rests, its TUI is closed and opened again on the same
    conversation, as a person does to pick up a new release: Claude
    ``--resume`` and Codex ``resume`` through their session pickers (the
    selection keys pass through the session shell when one is used), OpenCode
    ``--session <id>``. The child finishes after the reopened session is idle.

``idle-wake-restart-continue``
    The same, reopening the latest conversation with no picker (Claude
    ``--continue``, Codex ``resume --last``, OpenCode ``--continue``).

``idle-wake-restart-peer``
    The TUI is reopened through its picker under the session shell, then another
    session's ``peer-steward prompt`` must reach the resting session (the shell
    host's idle-and-empty judgment after keys typed into the picker).

``idle-wake-background-launch``
    The launching tool call returns before the child is registered (Claude: a
    Bash call the TUI runs in the background; Codex and OpenCode: a detached
    registration), and the session ends its turn with no further tool call.

Synthetic data stays under the fixture root, as in ``harness_behavior_cases_a``.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_a import (  # noqa: E402
    CompletionReceiptScenario, _carrying, _helper, _install_helper, _shell, _summary,
)


NATIVE_WAKE = "Hearting dispatch completed"


def _wake_source(text: str) -> str:
    """Which carrier put the receipt in front of the model (diagnostics only)."""
    if NATIVE_WAKE in text or "Stop hook feedback" in text:
        return "claude-async-rewake"
    if "(peer-from:" in text or "Hearting runtime" in text:
        return "runtime-message-or-sweep"
    return "other"


def _main_module(run):
    """The module whose ``Run`` executes: it holds ``--source`` and ``--session-shell``."""
    module = sys.modules.get(type(run).__module__)
    if module is None or not hasattr(module, "SESSION_SHELL"):
        module = sys.modules["harness_behavior_scenarios"]
    return module


class _IdleWake(CompletionReceiptScenario):
    """Register, end the turn, optionally disturb the resting session, then complete."""

    WAIT = 180  # long enough for the shared obligation runner's fallback to show up

    def disturb(self, run):
        """What happens to the resting session between register and complete."""
        return None

    def register_reply(self, run):
        return Reply(shell=_shell(run, *self.register_args()))

    def registered(self, run) -> bool:
        return any("HB-REGISTERED" in r["text"] for r in run.main_requests())

    def script(self, run):
        base = super().script(run)

        def answer(record, earlier):
            last = record["last_text"]
            if (record["last_kind"] != "tool" and "SCENARIO-RECEIPT start" in last
                    and not any(r["reply"].get("shell") or r["reply"].get("tool") for r in earlier)):
                return self.register_reply(run)
            return base(record, earlier)
        return answer

    def drive(self, run):
        run.prompt(self.PROMPT)
        run.wait_idle(8, 120)
        if not wait_for(lambda: self.registered(run), 60, 1):
            run.note("the register call never reached the model or the registry")
            return "inconclusive"
        requests = run.main_requests()
        run.evidence["register_output"] = next(
            (re.search(r"HB-REGISTERED[^\n\"]*", r["text"]).group(0)
             for r in requests if "HB-REGISTERED" in r["text"]), None)
        time.sleep(4)
        disturbed = self.disturb(run)
        if disturbed is not None:
            run.evidence["disturb"] = disturbed
            if disturbed.get("ok") is False:
                run.note("the disturbance could not be applied: " + str(disturbed.get("reason")))
                return "inconclusive"
        run.evidence["probe_before_completion"] = _helper(run, "probe")
        before = len(run.main_requests())
        run.evidence["requests_before_completion"] = before
        completed_at = time.time()
        run.evidence["complete"] = _helper(run, "complete")
        woke = wait_for(lambda: _carrying(run.main_requests(), self.attempt, before), self.WAIT, 1)
        run.wait_idle(8, 60)
        requests = run.main_requests()
        run.evidence["probe_after_wait"] = _helper(run, "probe")
        if woke:
            first = woke[0]
            run.evidence["wake_request"] = _summary(first)
            run.evidence["wake_source"] = _wake_source(first["last_text"])
            run.evidence["seconds_after_completion"] = round(first["at"] - completed_at, 1)
            ran = any(f"{self.NEXT}-42" in r["text"] for r in requests[first["main_index"]:])
            run.evidence["next_action_ran"] = ran
            run.note(f"woken {run.evidence['seconds_after_completion']}s after completion "
                     f"by {run.evidence['wake_source']}"
                     + ("; next action ran" if ran else "; next action did not run"))
            return "pass" if ran else "fail"
        count = len(requests)
        run.prompt(self.PROBE)
        run.wait_idle(8, 60)
        later = _carrying(run.main_requests(), self.attempt, count)
        run.evidence["fallback_next_prompt"] = bool(later)
        run.note(f"no wake within {self.WAIT}s"
                 + ("; receipt delivered at the next typed prompt" if later
                    else "; receipt not delivered at the next prompt either"))
        return "gap"


class IdleWakeFrameScenario(_IdleWake):
    name = "idle-wake-frame"
    summary = "A registered frame leg finishes while the parent rests; the parent is woken."

    def register_args(self):
        return ("register", "frame")


class IdleWakeReinstallScenario(_IdleWake):
    name = "idle-wake-reinstall"
    summary = "Hearting is installed again while the parent rests; its finished child still wakes it."

    def disturb(self, run):
        again = run.home.install()
        time.sleep(4)
        return {"ok": again.get("exit") == 0, "reason": f"install-exit-{again.get('exit')}",
                "install_seconds": again.get("seconds")}


EXIT_COMMANDS = {"claude": "/exit", "codex": "/quit", "opencode": "/exit"}
PICKER_MARKERS = {"claude": ("resume", "session", "search"), "codex": ("resume", "session")}


class IdleWakeRestartScenario(_IdleWake):
    name = "idle-wake-restart"
    summary = ("The resting parent's TUI is closed and reopened on the same conversation; "
               "its child then finishes and the reopened parent is woken.")

    def native_resume(self, run, binary, model) -> list[str]:
        """Claude and Codex through their session pickers; OpenCode has no CLI
        picker, so its explicit session selector."""
        if run.harness == "opencode":
            state = json.loads((run.home.root / "hb_fx_state.json").read_text(encoding="utf-8"))
            return [binary, "--standalone", "--session", state["session_id"]]
        return {"claude": [binary, "--model", model, "--resume"],
                "codex": [binary, "--no-alt-screen", "resume"]}[run.harness]

    picker = True

    def resume_argv(self, run) -> list[str]:
        main = _main_module(run)
        native = self.native_resume(run, run.binary, main.FIXTURE_MODEL)
        if main.SESSION_SHELL:
            return [sys.executable, str(main.SOURCE / "utilities" / "session-shell" / "entry.py"),
                    run.harness, *native]
        return native

    def disturb(self, run):
        tui = run.tui
        if not hasattr(tui, "_tmux"):
            return {"ok": False, "reason": "restart needs the private tmux surface"}
        typed = tui.type_text(EXIT_COMMANDS[run.harness])
        if not wait_for(lambda: "HEARTING-SCENARIO-TUI-EXIT=" in tui.screen(), 30, 0.5):
            tui.key("C-c")
            time.sleep(0.5)
            tui.key("C-c")
            if not wait_for(lambda: "HEARTING-SCENARIO-TUI-EXIT=" in tui.screen(), 20, 0.5):
                return {"ok": False, "reason": "the TUI did not exit", "exit_typed": typed}
        closed_at = time.time()
        time.sleep(2)
        run.evidence["probe_after_exit"] = _helper(run, "probe")
        argv = self.resume_argv(run)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(tui.env.items()))
        command = (f"cd {shlex.quote(str(tui.cwd))} && env -i {exports} "
                   + " ".join(shlex.quote(a) for a in argv)
                   + "; echo HEARTING-SCENARIO-TUI-EXIT=$?; sleep 3600")
        tui._tmux("respawn-pane", "-k", "-t", "scenario", "sh", "-c", command, check=True)
        picked = None
        markers = PICKER_MARKERS.get(run.harness) if self.picker else None
        if markers:
            # The picker lists this fixture's one conversation first: choose it.
            wait_for(lambda: all(m in tui.screen().lower() for m in markers[:1]), 45, 0.5)
            tui.settle(2, 20)
            picked = tui.screen()[-1500:]
            tui.key("Enter")
        ready = wait_for(lambda: tui.alive() and run._ready(tui.screen()), 90, 0.5)
        tui.settle(3, 45)
        time.sleep(8)  # the reopened session's start hooks run and it rests
        return {"ok": bool(ready), "reason": None if ready else "reopened TUI not ready",
                "closed_at": closed_at, "resume_argv": argv[-3:], "picker_screen": picked}


class IdleWakeRestartContinueScenario(IdleWakeRestartScenario):
    name = "idle-wake-restart-continue"
    summary = ("As idle-wake-restart, but the TUI reopens its latest conversation with no "
               "picker: Claude --continue, Codex resume --last, OpenCode --continue.")
    picker = False

    def native_resume(self, run, binary, model) -> list[str]:
        return {"claude": [binary, "--model", model, "--continue"],
                "codex": [binary, "--no-alt-screen", "resume", "--last"],
                "opencode": [binary, "--standalone", "--continue"]}[run.harness]


class IdleWakeRestartPeerScenario(IdleWakeRestartScenario):
    name = "idle-wake-restart-peer"
    summary = ("The parent's TUI is reopened through its picker (keys typed before the "
               "session started), then another session's peer prompt must reach it idle.")
    PROMPT = "SCENARIO-PEER first turn"

    def __init__(self):
        super().__init__()
        self.marker = "HB-IDLE-PEER-" + uuid.uuid4().hex[:8]

    def prepare(self, run):
        return None

    def script(self, run):
        def answer(record, earlier):
            if self.marker in record["last_text"] and not any(
                    "HB-PEER-SEEN" in (r["reply"].get("shell") or "") for r in earlier):
                return Reply(shell="echo HB-PEER-SEEN-$((40+2))")
            if record["last_kind"] == "tool":
                return Reply("Done.")
            return Reply("Noted; resting.")
        return answer

    def _session_id(self, run) -> str:
        if run.harness == "claude":
            found = sorted(Path(run.home.env["CLAUDE_CONFIG_DIR"], "projects").glob("*/*.jsonl"),
                           key=lambda path: path.stat().st_mtime)
            return found[-1].stem if found else ""
        return ""

    def native_resume(self, run, binary, model) -> list[str]:
        if run.harness == "opencode":
            return [binary, "--standalone", "--session", self.sid]
        return super().native_resume(run, binary, model)

    def drive(self, run):
        run.prompt(self.PROMPT)
        run.wait_idle(6, 90)
        self.sid = self._session_id(run)
        run.evidence["session_id"] = self.sid
        if not self.sid:
            run.note("no session id for this harness in the fixture")
            return "inconclusive"
        disturbed = self.disturb(run)
        run.evidence["disturb"] = disturbed
        if not disturbed.get("ok"):
            return "inconclusive"
        body = run.home.root / "peer-body.md"
        body.write_text(f"{self.marker} please acknowledge with one shell call.\n", encoding="utf-8")
        before = len(run.main_requests())
        sent_at = time.time()
        main = _main_module(run)
        sent = subprocess.run([sys.executable, str(main.SOURCE / "utilities/peer-steward.py"), "prompt",
                               f"{run.harness}:{self.sid}", "--body-file", str(body)],
                              env=run.home.env, cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=120)
        run.evidence["peer_prompt"] = {"exit": sent.returncode, "stdout": sent.stdout[-600:],
                                       "stderr": sent.stderr[-600:]}
        woke = wait_for(lambda: [r for r in run.main_requests()[before:]
                                 if self.marker in r["last_text"]], 150, 1)
        run.wait_idle(6, 45)
        if woke:
            run.evidence["seconds_after_send"] = round(woke[0]["at"] - sent_at, 1)
            ran = any("HB-PEER-SEEN-42" in r["text"] for r in run.main_requests())
            run.note(f"peer prompt reached the idle reopened session "
                     f"{run.evidence['seconds_after_send']}s after send"
                     + ("; its action ran" if ran else "; its action did not run"))
            return "pass" if ran else "fail"
        run.note("peer prompt did not reach the idle reopened session within 150s: "
                 + sent.stdout.strip().splitlines()[-1][:200] if sent.stdout.strip() else "no output")
        return "gap"


class IdleWakeBackgroundLaunchScenario(_IdleWake):
    name = "idle-wake-background-launch"
    summary = ("The child registers only after the launching tool call returned and the turn "
               "ended; its completion still wakes the resting parent.")
    PROMPT = "SCENARIO-RECEIPT start the registered child and end your turn"

    def prepare(self, run):
        super().prepare(run)
        conf = run.home.root / "hb_fx.json"
        data = json.loads(conf.read_text(encoding="utf-8"))
        data["delay_seconds"] = 8
        conf.write_text(json.dumps(data), encoding="utf-8")

    def register_reply(self, run):
        command = _shell(run, "register", "delay")
        if run.harness == "claude":
            return Reply(tool=("Bash", {"command": command, "run_in_background": True,
                                        "description": "Start the registered child"}))
        # Its own session, so the tool's process-group cleanup does not take it down.
        detached = f"setsid -f {command} >/dev/null 2>&1 </dev/null; echo HB-REGISTERING-LATER"
        return Reply(shell=detached)

    def registered(self, run) -> bool:
        state = run.home.root / "hb_fx_state.json"
        return state.exists()

    def script(self, run):
        base = super().script(run)

        def answer(record, earlier):
            if record["last_kind"] == "tool" and "HB-REGISTERING-LATER" in record["last_text"]:
                return Reply("The child will register shortly; ending my turn.")
            if (record["last_kind"] != "tool" and "HB-REGISTERED" in record["last_text"]
                    and self.attempt not in record["text"]):
                # Claude's background-task notification: acknowledge and end the turn.
                return Reply("The child is registered; waiting for its completion.")
            return base(record, earlier)
        return answer


SCENARIOS = [IdleWakeFrameScenario, IdleWakeReinstallScenario, IdleWakeRestartScenario,
             IdleWakeRestartContinueScenario, IdleWakeRestartPeerScenario,
             IdleWakeBackgroundLaunchScenario]
