#!/usr/bin/env python3
"""In-turn delivery and steward-watch scenarios for ``harness_behavior_scenarios.py``.

They measure the four behaviour differences the Codex in-turn carrier review
(RV-PR1435) left open, on the real TUIs through the loopback model:

``notice-mid-turn``
    An ordinary session notice (a report notice) is written for this session
    while it runs a shell step.  Pass: the notice reaches the model in the same
    turn, at the tool boundary that follows.  ``gap``: it only arrives at the
    next prompt (or an idle wake).

``completion-mid-turn`` / ``completion-mid-turn-nosocket``
    A registered child completes while the parent is in the middle of a turn.
    Pass: the receipt reaches the model in that turn (tool boundary or turn-end
    continuation) and the model's next action runs, with no typed prompt.  The
    ``-nosocket`` variant starts Codex with ``--no-daemon`` so the App Server
    queue socket does not exist (on Claude it is the same as the base case).

``follow-through-unrelated``
    A completion that names a next action arrives in the turn, the model runs
    an unrelated tool, then gives its final answer.  Pass: the session is asked
    again for the owed action (Claude's rewake, Codex's one follow-through pass)
    and the action runs.  ``gap``: the turn ends with the action owed.

``watch-wake``
    Needs a private Herdr server (``--surface herdr``).  The steward session arms
    a real ``peer-steward watch`` on a second agent and obeys the printed
    ``parent_next``: ``end-turn`` ends the turn, ``bounded-wait`` runs the printed
    join in the foreground.  The target then works and finishes.  Pass: the
    steward ended its turn (it is free for the person) and the watch receipt
    woke it with no typed prompt.  ``fail``: the line said ``end-turn`` and
    nothing woke it.  ``gap``: the steward had to hold a foreground wait.

Synthetic data stays under the fixture root: registry rows and pending records
through the production helpers of ``harness_behavior_cases_a``, and notices
through ``session_notice.notify``.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, PrivateTui, harness_argv, wait_for  # noqa: E402
from harness_behavior_cases_a import _helper, _install_helper, _shell, _summary  # noqa: E402
from harness_behavior_cases_b import fixture_python, long_shell, session_id  # noqa: E402
from dispatch_contract_roots import stable_state_root  # noqa: E402


NOTICE_CODE = r"""
import session_notice
record = session_notice.notify(args["harness"], args["sid"], key=args["key"], subject="report",
                               text=args["text"], required_action="read-notice",
                               jobs=os.environ["AGENT_DISPATCH_JOBS"])
print(json.dumps({"delivery_id": (record or {}).get("delivery_id"),
                  "recipient_kind": (record or {}).get("recipient_kind")}))
"""

RECORD_CODE = r"""
import dispatch_pending_delivery as pending
from pathlib import Path
root = Path(os.environ["AGENT_DISPATCH_JOBS"]).parent
rows = []
for path in sorted(root.glob("pending-delivery/*/delivery-*.json")):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    rows.append({k: data.get(k) for k in ("delivery_id", "recipient_kind", "state", "acked_by")})
print(json.dumps({"records": rows}))
"""

SOCKET_CODE = r"""
from codex_queue_dispatch import queue_endpoint_present, resolve_queue_socket
try:
    present = queue_endpoint_present()
except Exception as exc:  # an older tree has no helper
    present = "unknown: %s" % type(exc).__name__
print(json.dumps({"socket": str(resolve_queue_socket()), "present": present}))
"""


def _first_with(run, needle, start=0):
    for record in run.main_requests()[start:]:
        if needle in record["text"]:
            return record
    return None


def _tool_result_index(run, marker):
    return next((r["main_index"] for r in run.main_requests()
                 if r["last_kind"] == "tool" and marker in r["last_text"]), None)


# --------------------------------------------------------------------------
# notice-mid-turn
# --------------------------------------------------------------------------

class NoticeMidTurnScenario(Scenario):
    name = "notice-mid-turn"
    summary = ("An ordinary session notice written during a shell step reaches the model "
               "at the next tool boundary of the same turn.")

    def __init__(self):
        nonce = uuid.uuid4().hex[:8]
        self.token = f"SCENARIO-NOTICE-MIDTURN-{nonce}"
        self.step = f"SCENARIO-NOTICE-STEP-{nonce}"

    def script(self, run):
        def answer(record, earlier):
            shells = [r for r in earlier if r["reply"]["shell"]]
            if not shells and record["last_kind"] != "tool" and "SCENARIO-NOTICE run" in record["text"]:
                return Reply(shell=long_shell(f"sleep 14; echo {self.step}"))
            return Reply(f"notice noted {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-NOTICE run the step")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        time.sleep(2)
        ev["notice"] = fixture_python(run, NOTICE_CODE, {
            "harness": run.harness, "sid": sid, "key": "midturn-" + self.token,
            "text": f"{self.token} worker finished task T-1"})
        tool_index = wait_for(lambda: _tool_result_index(run, self.step), 60)
        ev["tool_result_request"] = tool_index
        run.wait_idle(6, 60)
        hit = _first_with(run, self.token)
        if hit is not None and tool_index is not None and hit["main_index"] == tool_index:
            ev["carrier"] = "same-turn-tool-boundary"
        elif hit is not None:
            ev["carrier"] = "later-request-without-prompt"
        else:
            typed = len(run.main_requests())
            run.prompt("SCENARIO-NOTICE-NEXT anything new?")
            run.wait_idle(6, 60)
            later = _first_with(run, self.token, typed)
            ev["carrier"] = "next-prompt" if later else "not-delivered"
        ev["records"] = fixture_python(run, RECORD_CODE)
        run.note(f"notice carrier={ev['carrier']}")
        return {"same-turn-tool-boundary": "pass", "later-request-without-prompt": "gap",
                "next-prompt": "gap"}.get(ev["carrier"], "fail")


# --------------------------------------------------------------------------
# completion-mid-turn
# --------------------------------------------------------------------------

class CompletionMidTurnScenario(Scenario):
    name = "completion-mid-turn"
    summary = ("A registered child completes while the parent is mid-turn; the receipt "
               "reaches that turn and the next action runs without a typed prompt.")
    carrier = "parent_completion"
    NEXT = "HB-NEXT-ACTION"
    codex_argv: tuple = ()

    def __init__(self):
        self.attempt = "att-hbfx-" + uuid.uuid4().hex[:20]
        self.step = "SCENARIO-MIDTURN-STEP-" + uuid.uuid4().hex[:6]

    def extra_argv(self, harness):
        return list(self.codex_argv) if harness == "codex" else []

    def script(self, run):
        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            shells = [r["reply"].get("shell") or "" for r in earlier]
            done_next = any(self.NEXT in s for s in shells)
            if self.attempt in record["text"] and not done_next and kind != "user" or (
                    self.attempt in last and not done_next):
                return Reply(shell=f"echo {self.NEXT}-$((20+22)) --attempt-id {self.attempt}")
            if kind == "tool" and self.NEXT in last:
                return Reply("Next action finished.")
            if kind == "tool" and "HB-REGISTERED" in last and not any(self.step in s for s in shells):
                return Reply(shell=long_shell(f"sleep 12; echo {self.step}"))
            if kind == "tool":
                return Reply("Step finished; nothing else to do.")
            if "SCENARIO-MIDTURN start" in last and not shells:
                return Reply(shell=_shell(run, "register", "nocarrier"))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        _install_helper(run, self.attempt)

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-MIDTURN start the registered child, then run the step")
        registered = wait_for(lambda: _first_with(run, "HB-REGISTERED"), 90)
        if not registered:
            run.note("the register shell call never reached the model")
            return "inconclusive"
        ev["queue_socket"] = fixture_python(run, SOCKET_CODE)
        # The step shell call is now running (12 s): the child completes inside it.
        time.sleep(4)
        ev["complete"] = _helper(run, "complete")
        completed_at = time.time()
        step_index = wait_for(lambda: _tool_result_index(run, self.step), 60)
        ev["step_result_request"] = step_index
        run.wait_idle(8, 90)
        requests = run.main_requests()
        # The first request after the register result whose text carries the
        # receipt: a tool-boundary context, a turn-end continuation or a wake.
        hit = next((r for r in requests[registered["main_index"] + 1:]
                    if self.attempt in r["text"]), None)
        ran = any(f"{self.NEXT}-42" in r["text"] for r in requests)
        ev["next_action_ran"] = ran
        if hit is not None:
            ev["receipt_request"] = _summary(hit)
            ev["seconds_after_completion"] = round(hit["at"] - completed_at, 1)
            ev["carrier"] = ("same-turn-tool-boundary"
                             if step_index is not None and hit["main_index"] == step_index
                             else "turn-end-or-wake")
        else:
            typed = len(requests)
            run.prompt("SCENARIO-MIDTURN-NEXT anything new?")
            run.wait_idle(8, 60)
            later = next((r for r in run.main_requests()[typed:] if self.attempt in r["text"]), None)
            ev["carrier"] = "next-prompt" if later else "not-delivered"
            ran = any(f"{self.NEXT}-42" in r["text"] for r in run.main_requests())
            ev["next_action_ran"] = ran
        ev["probe"] = _helper(run, "probe")
        run.note(f"receipt carrier={ev['carrier']}; next action {'ran' if ran else 'did not run'}")
        if ev["carrier"] in ("same-turn-tool-boundary", "turn-end-or-wake"):
            return "pass" if ran else "fail"
        return "gap"


class CompletionMidTurnNoSocketScenario(CompletionMidTurnScenario):
    name = "completion-mid-turn-nosocket"
    summary = ("As completion-mid-turn, with Codex started without the App Server daemon "
               "(no queue socket).")
    codex_argv = ("--no-daemon",)


# --------------------------------------------------------------------------
# follow-through-unrelated
# --------------------------------------------------------------------------

class FollowThroughUnrelatedScenario(CompletionMidTurnScenario):
    name = "follow-through-unrelated"
    summary = ("A failed child's receipt (inspect-done-failure) arrives; the model runs an "
               "unrelated tool and ends. Pass: the session is asked again and the action runs.")
    FINAL = "Done; nothing else to do."

    def script(self, run):
        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            shells = [r["reply"].get("shell") or "" for r in earlier]
            done_next = any(self.NEXT in s for s in shells)
            finals = [r for r in earlier if r["reply"].get("text") == self.FINAL]
            if kind == "tool" and self.NEXT in last:
                return Reply("Next action finished.")
            if finals and not done_next and self.attempt in record["text"][-6000:] \
                    and record["main_index"] > finals[-1]["main_index"]:
                # Asked again after the final answer: now act on the owed receipt.
                return Reply(shell=f"echo {self.NEXT}-$((20+22)) --attempt-id {self.attempt}")
            if kind == "tool" and "HB-REGISTERED" in last and not any("ls -1" in s for s in shells):
                # A weak model: it ignores the receipt and runs something unrelated.
                return Reply(shell="ls -1 / | head -3")
            if kind == "tool" and not finals:
                return Reply(self.FINAL)
            if "SCENARIO-FOLLOW start" in last and not shells:
                return Reply(shell=_shell(run, "register", "nocarrier") + " && "
                             + _shell(run, "complete", "fail"))
            return Reply("Nothing new.")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-FOLLOW start the child and finish")
        final = wait_for(lambda: next((r for r in run.main_requests()
                                       if r["reply"].get("text") == self.FINAL), None), 120)
        if not final:
            run.note("the final answer never came")
            return "inconclusive"
        ev["receipt_in_turn_before_final"] = any(
            self.attempt in r["text"] and "inspect-done-failure" in r["text"]
            for r in run.main_requests()[1:final["main_index"] + 1])
        asked = wait_for(lambda: next((r for r in run.main_requests()[final["main_index"] + 1:]
                                       if self.attempt in r["text"][-6000:]), None), 60)
        run.wait_idle(8, 60)
        ran = any(f"{self.NEXT}-42" in r["text"] for r in run.main_requests())
        ev["next_action_ran"] = ran
        ev["probe"] = _helper(run, "probe")
        if asked:
            ev["asked_again_request"] = _summary(asked)
            ev["seconds_after_final"] = round(asked["at"] - final["at"], 1)
            run.note(f"asked again after the final answer; next action {'ran' if ran else 'did not run'}")
            return "pass" if ran else "fail"
        run.note("turn ended with the action owed; nothing asked again")
        return "gap"


# --------------------------------------------------------------------------
# native watch scenarios (private tmux, no Herdr server)

class WatchWakeScenario(Scenario):
    name = "watch-wake"
    summary = ("A native watch on a second isolated tmux session emits one meaningful receipt "
               "and wakes the steward after its turn ends.")
    surface = "tmux"
    NEXT = "HB-WATCH-NEXT"

    def __init__(self):
        self.target = "hbtarget" + uuid.uuid4().hex[:4]
        self.target_tui = None
        self.target_address = None

    def _answer(self, record, earlier):
        last, kind = record["last_text"], record["last_kind"]
        shells = [r["reply"].get("shell") or "" for r in earlier]
        # The steward TUI and the target TUI share this script. record["text"] is the whole
        # conversation, so a session is told apart by its own markers and answered from its
        # current input (last/kind), never from text an earlier turn of the same session left.
        steward = "SCENARIO-WATCH arm" in record["text"]
        if not steward and "SCENARIO-WATCH-TARGET" in record["text"]:
            if kind == "tool":
                return Reply("target turn done")
            if "SCENARIO-WATCH-TARGET WORK" in last:
                return Reply(shell="sleep 12; echo SCENARIO-WATCH-TARGET-DONE")
            return Reply("target initialized")
        if kind == "tool" and self.NEXT in last:
            return Reply("Watch handled.")
        armed = [r for r in earlier if "state=armed" in r["last_text"]]
        receipt_seen = bool(armed) and re.search(
            r"state=(?:idle|done|session-ended|session-replaced)\b[^\n]*watch_id=|"
            r"watch_id=\S+ state=(?:idle|done|session-ended|session-replaced)\b",
            record["text"]) is not None
        if receipt_seen and not any(self.NEXT in shell for shell in shells):
            return Reply(shell=f"echo {self.NEXT}-42")
        if kind == "tool" and "state=armed" in last:
            match = re.search(r"parent_next=(\S+).*?parent_next_command=(.*)$", last, re.S)
            if match and match.group(1) == "bounded-wait" and match.group(2).strip() not in ("", "-") \
                    and not any(" join " in shell for shell in shells):
                command = match.group(2).strip().splitlines()[0]
                return Reply(shell=long_shell(command, wait_ms=600000))
            return Reply("Watch armed; ending my turn as the line says.")
        if "SCENARIO-WATCH arm" in last and not any(
                "peer-steward.py" in shell and " watch " in shell for shell in shells):
            target = self.target_address or "claude:unresolved"
            return Reply(shell=f'python3 "$AGENT_HOME/utilities/peer-steward.py" watch {shlex.quote(target)} '
                               f'--until idle --timeout 240000')
        return Reply("Nothing new.")

    def script(self, run):
        return self._answer

    def prepare(self, run):
        """Start a second real runtime under its own private tmux server."""
        root = Path(run.home.root)
        argv = harness_argv(run.harness, run.binary)
        self.target_tui = PrivateTui(root, dict(run.home.env), argv, run.home.workspace)
        self.target_tui.start()
        ready = wait_for(lambda: self.target_tui.alive() and run._ready(self.target_tui.screen()), 90)
        if not ready:
            raise RuntimeError("native watch target TUI did not become ready")
        run._dismiss_startup(self.target_tui)
        if not self.target_tui.type_text("SCENARIO-WATCH-TARGET INIT"):
            raise RuntimeError("native watch target prompt was not accepted")
        if not self.target_tui.settle(quiet=2, timeout=45):
            raise RuntimeError("native watch target did not settle after initialization")
        address = self.target_tui._tmux(
            "display-message", "-p", "-t", "scenario", "#{pane_id}@#{socket_path}")
        self.target_address = (address.stdout or "").strip()
        if not self.target_address or address.returncode:
            raise RuntimeError("native watch target pane address unavailable")
        run.evidence["target_address"] = self.target_address

    def cleanup(self, run):
        if self.target_tui is not None:
            with contextlib.suppress(Exception):
                self.target_tui.stop()
            self.target_tui = None

    def drive(self, run):
        ev = run.evidence
        if self.target_tui is None or not self.target_address:
            run.note("private tmux target was not created")
            return "inconclusive"
        ev["target_prompt"] = self.target_tui.type_text("SCENARIO-WATCH-TARGET WORK")
        if not ev["target_prompt"]:
            run.note("target work prompt was not accepted")
            return "inconclusive"
        run.prompt("SCENARIO-WATCH arm a native watch on the target and follow parent_next")
        armed = wait_for(lambda: _first_with(run, "state=armed"), 90)
        if not armed:
            line = _first_with(run, "watch_id=")
            ev["watch_output"] = (line or {}).get("last_text", "")[-600:]
            run.note("native watch was not armed")
            return "inconclusive"
        text = armed["last_text"]
        ev["watch_line"] = (re.search(r"watch_id=[^\n]*state=armed[^\n]*", text) or [""])[0][:300]
        ev["parent_next"] = (re.search(r"parent_next=(\S+)", text) or [None, None])[1]
        ev["parent_next_reason"] = (re.search(r"parent_next_reason=(\S+)", text) or [None, None])[1]
        before = len(run.main_requests())
        ended = wait_for(lambda: any(r["reply"].get("text", "").startswith("Watch armed")
                                     for r in run.main_requests()), 20)
        ev["steward_ended_turn"] = bool(ended)
        completed = wait_for(lambda: "SCENARIO-WATCH-TARGET-DONE" in self.target_tui.screen(), 30, 0.2)
        ev["target_completed"] = bool(completed)
        woke = wait_for(lambda: any(self.NEXT in (r["reply"].get("shell") or "")
                                    for r in run.main_requests()[before:]), 150, 1)
        ev["next_action_ran"] = any(f"{self.NEXT}-42" in r["text"] for r in run.main_requests())
        if woke and ended:
            run.note(f"steward ended its turn ({ev['parent_next']}) and the native receipt woke it")
            return "pass" if ev["next_action_ran"] else "fail"
        run.note(f"parent_next={ev['parent_next']}; no native wake after target activity")
        return "fail" if ev["parent_next"] == "end-turn" else "gap"


class NativeWatchEndScenario(WatchWakeScenario):
    name = "native-watch-end"
    summary = "An exact native session end writes one receipt; unknown states remain unnotified."

    def drive(self, run):
        ev = run.evidence
        if self.target_tui is None or not self.target_address:
            return "inconclusive"
        ev["target_prompt"] = self.target_tui.type_text("SCENARIO-WATCH-TARGET WORK")
        if not ev["target_prompt"]:
            return "inconclusive"
        run.prompt("SCENARIO-WATCH arm a native watch on the target and follow parent_next")
        armed = wait_for(lambda: _first_with(run, "state=armed"), 90)
        if not armed:
            run.note("native watch was not armed")
            return "inconclusive"
        text = armed["last_text"]
        ev["watch_line"] = (re.search(r"watch_id=[^\n]*state=armed[^\n]*", text) or [""])[0][:300]
        watch_id = (re.search(r"watch_id=([0-9a-f]{16})", text) or [None, None])[1]
        before = len(run.main_requests())
        self.target_tui.stop()
        self.target_tui = None
        receipt = wait_for(lambda: self._read_watch_receipt(run, watch_id), 45, 0.2)
        ev["receipt"] = receipt
        ev["receipt_count"] = 1 if receipt else 0
        woke = wait_for(lambda: any(self.NEXT in (r["reply"].get("shell") or "")
                                    for r in run.main_requests()[before:]), 120, 1)
        ev["next_action_ran"] = any(f"{self.NEXT}-42" in r["text"] for r in run.main_requests())
        if receipt and receipt.get("state") == "session-ended" and woke:
            return "pass" if ev["next_action_ran"] else "fail"
        run.note("target end did not produce one exact session-ended receipt and native wake")
        return "fail"

    @staticmethod
    def _read_watch_receipt(run, watch_id):
        if not watch_id:
            return None
        # The fixture env never sets AGENT_PEER_LEDGER_ROOT, so resolve the receipt root the way
        # peer-message.peer_state_root() does inside the fixture: the override when present, else
        # the stable per-user state root under that env.
        env = run.home.env
        override = env.get("AGENT_PEER_LEDGER_ROOT")
        state = Path(override) if override else stable_state_root(env)
        path = state / "peer-watches" / f"{watch_id}.receipt.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


SCENARIOS = [NoticeMidTurnScenario, CompletionMidTurnScenario, CompletionMidTurnNoSocketScenario,
             FollowThroughUnrelatedScenario, WatchWakeScenario, NativeWatchEndScenario]
