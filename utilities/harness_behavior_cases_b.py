#!/usr/bin/env python3
"""Behaviour scenarios B: report arrival, native question answer, handoff accept.

Loaded by ``harness_behavior_scenarios.py`` (``_load_extra``).  Each scenario runs on
the real Claude, Codex and OpenCode TUIs in the base harness's throwaway HOME; the
oracle is what reached the loopback model plus the Hearting state the hooks wrote.

* ``report-arrival``  a peer report addressed to this session arrives (a) while the
  session is between tool calls and (b) while it is idle.  Records whether it was
  injected mid-work, woke the idle session, or only arrived with the next prompt.
* ``question-answer`` the model calls the native question tool; Fleet's decision
  wait (interaction sidecar, or the Codex rollout fallback) is read before and after
  the user answers in the TUI; the answer must reach the model.
* ``handoff-accept``  a typed cross-seat session-tidy handoff (인계) addressed to this
  session arrives; the model runs the exact accept command the body carries; the
  acceptance must be recorded and the reply to the predecessor scheduled.

Synthetic peer messages are written through the shipped writer
(``peer-message.py prepare_peer_message``) inside the fixture environment, exactly as
``peer-steward.py prompt`` does before its transport step; no user session, herdr
server or shared daemon is touched.

The base fixture only knows shell tool calls.  ``NativeCall`` (a ``str`` so records
stay JSON) carries an arbitrary tool call through ``Reply(shell=...)``; the module
wraps ``_shell_call`` in every loaded copy of the base module to honour it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as base  # noqa: E402
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402


# --------------------------------------------------------------------------
# Native tool calls through the base fixture
# --------------------------------------------------------------------------

class NativeCall(str):
    """A tool call the fixture should emit instead of the shell tool.

    ``build(protocol, names) -> (tool_name, arguments)``.  The string value is a
    readable label for requests.json.
    """

    def __new__(cls, label, build):
        value = super().__new__(cls, label)
        value.build = build
        return value


TOOL_SCHEMAS: dict = {}


def _patch(module):
    if getattr(module, "_cases_b_patched", False) or not hasattr(module, "_shell_call"):
        return
    original_call = module._shell_call
    original_names = module._tool_names

    def shell_call(protocol, names, command):
        if isinstance(command, NativeCall):
            return command.build(protocol, names)
        return original_call(protocol, names, command)

    def tool_names(payload):
        for tool in payload.get("tools") or []:
            if isinstance(tool, dict):
                name = tool.get("name") or (tool.get("function") or {}).get("name")
                if name in ("AskUserQuestion", "request_user_input", "question",
                            "exec_command") and name not in TOOL_SCHEMAS:
                    TOOL_SCHEMAS[name] = tool
        return original_names(payload)

    original_argv = module.harness_argv

    def harness_argv(harness, binary):
        argv = original_argv(harness, binary)
        # Diagnostics only: OpenCode drops plugin app.log lines below DEBUG.
        if harness == "opencode" and os.environ.get("HEARTING_B_OPENCODE_DEBUG") == "1":
            argv = [*argv, "--log-level", "debug"]
        return argv

    module._shell_call = shell_call
    module._tool_names = tool_names
    module.harness_argv = harness_argv
    module._cases_b_patched = True


def _patch_loaded():
    for module in list(sys.modules.values()):
        if module is not None and hasattr(module, "ModelFixture") and hasattr(module, "_shell_call"):
            _patch(module)


_patch_loaded()


def long_shell(command: str, wait_ms: int = 30000) -> NativeCall:
    """A shell call whose command may run for tens of seconds on every harness."""
    def build(protocol, names):
        if protocol == "responses" and "exec_command" in names:
            return "exec_command", {"cmd": command, "yield_time_ms": wait_ms}
        name, args = _plain_shell(protocol, names, command)
        if protocol == "chat":
            args["timeout"] = max(wait_ms * 2, 60000)
        return name, args
    return NativeCall("long-shell: " + command, build)


def _plain_shell(protocol, names, command):
    if protocol == "chat":
        name = "shell" if "shell" in names else "bash"
        return name, {"command": command, "description": "scenario step"}
    if protocol == "anthropic":
        return "Bash", {"command": command, "description": "scenario step", "timeout": 120000}
    for name in ("exec_command", "shell_command", "shell", "local_shell"):
        if name in names:
            if name == "exec_command":
                return name, {"cmd": command}
            if name == "shell_command":
                return name, {"command": command}
            return name, {"command": ["bash", "-lc", command]}
    return "shell", {"command": ["bash", "-lc", command]}


QUESTION_TEXT = "Which branch should the scenario take?"
OPTION_A, OPTION_B = "Alpha-route", "Beta-route"


def native_question() -> NativeCall:
    def build(protocol, names):
        options = [{"label": OPTION_A, "description": "first scenario branch"},
                   {"label": OPTION_B, "description": "second scenario branch"}]
        if protocol == "anthropic":
            return "AskUserQuestion", {"questions": [{
                "question": QUESTION_TEXT, "header": "Branch", "multiSelect": False,
                "options": options}]}
        if protocol == "responses":
            return "request_user_input", {"questions": [{
                "id": "branch", "header": "Branch", "question": QUESTION_TEXT,
                "options": options}]}
        return "question", {"questions": [{
            "question": QUESTION_TEXT, "header": "Branch", "multiple": False,
            "options": options}]}
    return NativeCall("native-question", build)


# --------------------------------------------------------------------------
# Fixture-side helpers (always run inside the fixture HOME environment)
# --------------------------------------------------------------------------

_PRELUDE = r"""
import importlib.util, json, os, sys
home = os.environ["AGENT_HOME"]
sys.path[:0] = [home + "/utilities", home + "/tools"]
def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, home + "/" + rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
"""


def fixture_python(run, code: str, payload=None, timeout: float = 60) -> dict:
    env = dict(run.home.env)
    done = subprocess.run([sys.executable, "-B", "-c", _PRELUDE + code,
                           json.dumps(payload or {})], env=env, cwd=str(run.home.workspace),
                          text=True, capture_output=True, timeout=timeout)
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    try:
        value = json.loads(lines[-1])
    except (ValueError, IndexError):
        value = {"exit": done.returncode, "stdout": done.stdout[-1500:],
                 "stderr": done.stderr[-1500:]}
    return value


def session_id(run, timeout: float = 30) -> str | None:
    """The TUI's own native session id, from the seat ledger the start/prompt hooks write."""
    root = Path(run.home.env["HARNESS_STATE_ROOT"]) / "session-tidy" / "sessions"

    def find():
        for path in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("harness") == run.harness and row.get("sid"):
                        return row["sid"]
            except (OSError, ValueError):
                continue
        return None
    sid = wait_for(find, timeout)
    if sid:
        run.evidence["session_id"] = sid
        run.evidence["session_id_source"] = "session-tidy seat ledger"
    return sid


SEND_CODE = r"""
pm = load("_peer_message_b", "utilities/peer-message.py")
steward = None
text, ref = pm.prepare_peer_message(args["body"], args["sender"], args["recipient"], defer=True,
                                    receipt="native-pending", handoff=args.get("handoff"))
row = pm._read_pending(ref)
obligation = None
if args.get("obligation"):
    try:
        steward = load("_peer_steward_b", "utilities/peer-steward.py")
        duty = steward._schedule_message_obligation(row, args["recipient"]["session_id"], None)
        obligation = bool(duty)
    except Exception as exc:
        obligation = "error: %s" % exc
print(json.dumps({"ref": ref, "text": text, "state": row.get("state"),
                  "receipt": row.get("receipt"), "obligation": obligation}))
"""

PENDING_CODE = r"""
pm = load("_peer_message_b", "utilities/peer-message.py")
row = pm._read_pending(args["ref"])
print(json.dumps({"state": (row or {}).get("state"), "receipt": (row or {}).get("receipt"),
                  "acceptance": (row or {}).get("acceptance")}, default=str))
"""

STEWARD_PROMPT_CODE = r"""
import subprocess
done = subprocess.run([sys.executable, home + "/utilities/peer-steward.py", "prompt",
                       args["target"], args["body"]], capture_output=True, text=True, timeout=40)
print(json.dumps({"exit": done.returncode, "stdout": done.stdout[-600:],
                  "stderr": done.stderr[-400:]}))
"""


def send_peer(run, body: str, *, handoff=None, sender=None, obligation=True) -> dict:
    sid = run.evidence.get("session_id")
    sender = sender or {"harness": "claude", "session_id": "fx-" + uuid.uuid4().hex[:12],
                        "name": "scenario-worker"}
    result = fixture_python(run, SEND_CODE, {
        "body": body, "sender": sender,
        "recipient": {"harness": run.harness, "session_id": sid},
        "handoff": handoff, "obligation": obligation})
    result["sent_at"] = time.time()
    result["sender"] = sender
    return result


def pending_state(run, ref: str) -> dict:
    return fixture_python(run, PENDING_CODE, {"ref": ref})


def requests_with(run, needle: str, start: int = 0) -> list[int]:
    return [r["main_index"] for r in run.main_requests()[start:] if needle in r["text"]]


def first_with(run, needle: str, start: int = 0):
    hits = requests_with(run, needle, start)
    return hits[0] if hits else None


def plugin_trace(run, pattern: str, limit: int = 12) -> list[str]:
    """OpenCode plugin peer-delivery trace lines (diagnostics only)."""
    hits = []
    for root in (Path(run.home.env["XDG_STATE_HOME"]), Path(run.home.env["XDG_DATA_HOME"])):
        for path in [*root.rglob("*.jsonl"), *root.rglob("opencode*.log")]:
            try:
                if path.stat().st_size > 4_000_000:
                    continue
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if pattern in line:
                        hits.append(line[-400:])
            except OSError:
                continue
    return hits[-limit:]


# --------------------------------------------------------------------------
# 1. report-arrival
# --------------------------------------------------------------------------

class ReportArrivalScenario(Scenario):
    name = "report-arrival"
    summary = ("A peer report reaches the session mid-work (between tool calls) and "
               "while idle (wake or next prompt).")

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.mid = f"SCENARIO-REPORT-MIDWORK-{self.nonce}"
        self.idle = f"SCENARIO-REPORT-IDLE-{self.nonce}"

    def script(self, run):
        def answer(record, earlier):
            shells = [r for r in earlier if r["reply"]["shell"]]
            if (not shells and record["last_kind"] != "tool"
                    and "SCENARIO-REPORT phase-one" in record["text"]):
                return Reply(shell=long_shell("sleep 14; echo SCENARIO-REPORT-STEP-DONE"))
            return Reply(f"report noted {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-REPORT phase-one: run the step")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        # What the real sender CLI does here without a herdr pane (diagnostic only).
        ev["steward_prompt_without_herdr"] = fixture_python(
            run, STEWARD_PROMPT_CODE, {"target": sid, "body": "SCENARIO-REPORT-STEWARD probe"})
        time.sleep(1)
        ev["send_midwork"] = send_peer(run, f"[report] {self.mid} worker finished task T-1")
        mid_ref = ev["send_midwork"].get("ref")
        # The shell step finishes ~14 s after it started; its result request follows.
        tool_index = wait_for(lambda: next((r["main_index"] for r in run.main_requests()
                                            if "SCENARIO-REPORT-STEP-DONE" in r["text"]), None), 60)
        ev["tool_result_request"] = tool_index
        run.wait_idle(6, 60)
        mid_hit = first_with(run, self.mid)
        ev["midwork_first_request"] = mid_hit
        ev["midwork_carrier"] = (
            "mid-work-injection" if mid_hit is not None and tool_index is not None and mid_hit == tool_index
            else "later-request" if mid_hit is not None else "not-delivered-yet")
        if mid_ref:
            ev["midwork_pending_after_turn"] = pending_state(run, mid_ref)

        # (b) idle: the session has finished its turn.
        before = len(run.main_requests())
        ev["send_idle"] = send_peer(run, f"[report] {self.idle} worker finished task T-2")
        idle_ref = ev["send_idle"].get("ref")
        woke = wait_for(lambda: first_with(run, self.idle, before) is not None, 50)
        typed_at = None
        if woke:
            ev["idle_carrier"] = "idle-wake"
        else:
            typed_at = len(run.main_requests())
            run.prompt("SCENARIO-REPORT phase-two: anything new?")
            run.wait_idle(6, 60)
            # An end-of-turn carrier (OpenCode session.idle) submits after the turn.
            wait_for(lambda: first_with(run, self.idle, typed_at) is not None, 20)
            if first_with(run, self.idle, typed_at) is not None:
                ev["idle_carrier"] = "next-prompt"
            else:
                ev["idle_carrier"] = "not-delivered"
        ev["idle_first_request"] = first_with(run, self.idle, before)
        if mid_hit is None:
            late = first_with(run, self.mid)
            ev["midwork_first_request"] = late
            if late is not None:
                ev["midwork_carrier"] = ("next-prompt" if typed_at is not None and late >= typed_at
                                         else "idle-wake")
        if idle_ref:
            ev["idle_pending_final"] = pending_state(run, idle_ref)
        if mid_ref:
            ev["midwork_pending_final"] = pending_state(run, mid_ref)
        if run.harness == "opencode":
            ev["opencode_origin"] = opencode_origin(run)
        run.note(f"mid-work={ev['midwork_carrier']} idle={ev['idle_carrier']}")
        # Mid-work: injected at the tool boundary, or only later (no mid-work carrier).
        mid_verdict = ("pass" if ev["midwork_carrier"] == "mid-work-injection" else
                       "gap" if ev["midwork_carrier"] in ("idle-wake", "next-prompt",
                                                          "later-request", "not-delivered-yet")
                       else "fail")
        # Idle: woken, or at least delivered with the next prompt; never is a failure
        # because every adapter ships an idle/next-prompt carrier.
        idle_verdict = "pass" if ev["idle_carrier"] in ("idle-wake", "next-prompt") else "fail"
        ev["verdict_midwork"], ev["verdict_idle"] = mid_verdict, idle_verdict
        for verdict in ("fail", "gap"):
            if verdict in (mid_verdict, idle_verdict):
                return verdict
        return "pass"


def opencode_origin(run) -> dict:
    """Where the OpenCode plugin runs and whether the TUI selection record exists."""
    root = run.home.root
    procs = []
    for pid in base.fixture_processes(root):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [a.decode(errors="replace") for a in argv if a]
        if words and Path(words[0]).name == "opencode":
            procs.append({"pid": pid, "argv": words[1:6]})
    records = Path(run.home.env["XDG_STATE_HOME"]) / "hearting" / "tui-identity"
    return {"opencode_processes": procs,
            "tui_identity_records": sorted(p.name for p in records.glob("*.json"))
            if records.is_dir() else None}


# --------------------------------------------------------------------------
# 2. question-answer
# --------------------------------------------------------------------------

WAIT_CODE = r"""
from fleet import interaction
out = {"sidecar_path": interaction.sidecar_path(args["sid"], args["harness"])}
out["sidecar"] = interaction.read_wait(args["sid"], args["harness"])
try:
    out["sidecar_raw"] = open(out["sidecar_path"], encoding="utf-8").read()[:600]
except OSError:
    out["sidecar_raw"] = None
if args["harness"] == "codex":
    try:
        from fleet.collectors import codex as rollouts
        found = rollouts.open_rollout_questions()
        out["codex_rollout_wait"] = None if found is None else {
            "complete": found.get("complete"), "pending": args["sid"] in (found.get("pending") or {})}
    except Exception as exc:
        out["codex_rollout_wait"] = "error: %s" % exc
print(json.dumps(out, default=str))
"""


class QuestionAnswerScenario(Scenario):
    name = "question-answer"
    summary = ("The model asks through the native question tool; Fleet shows a decision "
               "wait; the TUI answer clears it and reaches the model.")

    def script(self, run):
        def answer(record, earlier):
            asked = [r for r in earlier if r["reply"]["shell"] == "native-question"]
            if not asked and "SCENARIO-QUESTION" in record["text"] and record["last_kind"] != "tool":
                return Reply(text="I need one choice.", shell=native_question())
            return Reply(f"answer noted {record['main_index']}")
        return answer

    def prepare(self, run):
        if run.harness != "codex":
            return
        # Release activation (tools/install/runtime_activation.py:2101 _codex_feature_insert)
        # enables the Default-mode question tool for interactive homes; the source-tree
        # fixture install does not run that step, so apply the same one-line change.
        config = Path(run.home.env["CODEX_HOME"]) / "config.toml"
        text = config.read_text(encoding="utf-8")
        if "default_mode_request_user_input" not in text:
            if re.search(r"(?m)^\[features\]\s*$", text):
                text = re.sub(r"(?m)^\[features\]\s*$",
                              "[features]\ndefault_mode_request_user_input = true", text, count=1)
            else:
                text += "\n[features]\ndefault_mode_request_user_input = true\n"
            import tomllib
            tomllib.loads(text)
            config.write_text(text, encoding="utf-8")
            run.evidence["codex_feature_applied"] = "features.default_mode_request_user_input=true"

    def _wait(self, run):
        return fixture_python(run, WAIT_CODE, {"sid": run.evidence.get("session_id"),
                                               "harness": run.harness})

    @staticmethod
    def _waiting(state):
        if not isinstance(state, dict):
            return False
        rollout = state.get("codex_rollout_wait")
        return bool(state.get("sidecar")) or (isinstance(rollout, dict) and rollout.get("pending"))

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-QUESTION please ask me")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found")
            return "inconclusive"
        # The question form must be on screen before the wait is judged.
        shown = wait_for(lambda: OPTION_A in run.tui.screen(), 30)
        ev["question_on_screen"] = bool(shown)
        time.sleep(2)
        ev["wait_before"] = wait_for(lambda: (lambda s: s if self._waiting(s) else None)(
            self._wait(run)), 15) or self._wait(run)
        ev["screen_question"] = run.tui.screen()[-1500:]
        if not shown:
            run.note("question form never rendered")
            ev["tool_schema"] = TOOL_SCHEMAS.get({"claude": "AskUserQuestion",
                                                  "codex": "request_user_input",
                                                  "opencode": "question"}[run.harness])
            return "fail"
        before = len(run.main_requests())
        # Pick the first option.  Each TUI may need one confirmation more.
        keys_sent = []
        for keys in (["Enter"], ["Enter"], ["Enter"], ["Tab", "Enter"]):
            run.tui.key(*keys)
            keys_sent.append("+".join(keys))
            if wait_for(lambda: len(run.main_requests()) > before, 8):
                break
        ev["answer_keys"] = keys_sent
        run.wait_idle(5, 60)
        ev["wait_after"] = self._wait(run)
        answered = [r["main_index"] for r in run.main_requests()[before:]
                    if OPTION_A in r.get("last_text", "")]
        ev["answer_request"] = answered[0] if answered else None
        ev["answer_last_text"] = next((r["last_text"][-500:] for r in run.main_requests()[before:]
                                       if OPTION_A in r.get("last_text", "")), None)
        wait_seen = self._waiting(ev["wait_before"])
        sidecar = (ev["wait_before"] or {}).get("sidecar") if isinstance(ev["wait_before"], dict) else None
        ev["fleet_wait_kind"] = (sidecar or {}).get("kind") or (
            "decision(codex-rollout)" if wait_seen else None)
        wait_cleared = not self._waiting(ev["wait_after"])
        ev["fleet_wait_seen"] = bool(wait_seen)
        ev["fleet_wait_cleared"] = bool(wait_cleared)
        run.note(f"wait_seen={bool(wait_seen)} kind={ev['fleet_wait_kind']} cleared={wait_cleared} "
                 f"answer_request={ev['answer_request']}")
        if ev["answer_request"] is None:
            return "fail"
        if wait_seen and wait_cleared:
            # A question is a decision wait; any other kind misreports it on the board.
            return "pass" if str(ev["fleet_wait_kind"]).startswith("decision") else "fail"
        return "gap" if not wait_seen else "fail"


# --------------------------------------------------------------------------
# 3. handoff-accept (인계 수락)
# --------------------------------------------------------------------------

class HandoffAcceptScenario(Scenario):
    name = "handoff-accept"
    summary = ("A typed session-tidy handoff (인계) reaches the successor session, which "
               "runs the sealed accept command; acceptance and the reply are recorded.")

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.card = f"SCENARIO-HANDOFF-CARD-{self.nonce}"
        self.ref = None

    def script(self, run):
        def answer(record, earlier):
            ran = [r for r in earlier if "handoff accept" in str(r["reply"]["shell"] or "")]
            if not ran and record["last_kind"] != "tool" and self.ref and self.card in record["text"]:
                match = re.search(r'python3 "\$AGENT_HOME/utilities/session_tidy\.py" handoff accept '
                                  r'([0-9a-f]{32})[^\n`]*', record["text"])
                command = match.group(0) if match else (
                    f'python3 "$AGENT_HOME/utilities/session_tidy.py" handoff accept {self.ref}')
                return Reply(text="Card read; accepting.", shell=command)
            if not ran and "SCENARIO-HANDOFF accept-now" in record["text"] and self.ref \
                    and record["last_kind"] != "tool":
                return Reply(shell=f'python3 "$AGENT_HOME/utilities/session_tidy.py" handoff accept {self.ref}')
            return Reply(f"handoff noted {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-HANDOFF hello")
        if not run.wait_requests(1, 60):
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found")
            return "inconclusive"
        run.wait_idle(5, 40)
        pred_sid = "fx-pred-" + uuid.uuid4().hex[:12]
        predecessor = {"server": "default", "pane": "", "harness": "claude",
                       "session_id": pred_sid, "name": None, "foreground": None}
        successor = {"server": "default", "pane": "", "harness": run.harness,
                     "session_id": sid, "name": None, "foreground": None}
        card = f"{self.card}\n이전 작업: scenario task T-9 is half done; continue with step 3."
        import hashlib
        context = {"card_generation": 1,
                   "card_body_sha256": hashlib.sha256(card.encode()).hexdigest(),
                   "target": "", "predecessor": predecessor, "successor": successor,
                   "requester": {"harness": run.harness, "session_id": sid, "pane": "",
                                 "server": "default"}}
        before = len(run.main_requests())
        ev["send_handoff"] = send_peer(run, card, handoff=context,
                                       sender={"harness": "claude", "session_id": pred_sid,
                                               "name": "scenario-predecessor"})
        self.ref = ev["send_handoff"].get("ref")
        if not self.ref:
            run.note("handoff row could not be prepared")
            return "inconclusive"
        woke = wait_for(lambda: first_with(run, self.card, before) is not None, 45)
        typed = None
        if woke:
            ev["handoff_carrier"] = "idle-wake"
        else:
            typed = len(run.main_requests())
            run.prompt("SCENARIO-HANDOFF anything new?")
            run.wait_idle(6, 60)
            ev["handoff_carrier"] = ("next-prompt" if first_with(run, self.card, typed) is not None
                                     else "not-delivered")
        run.wait_idle(6, 60)
        hit = first_with(run, self.card, before)
        ev["handoff_first_request"] = hit
        if hit is not None:
            text = run.main_requests()[hit]["text"]
            ev["accept_command_in_body"] = "handoff accept " + self.ref in text
            ev["receiver_guidance_exact_command"] = "--session-id" in text and "handoff accept" in text
        ran_accept = any("handoff accept" in str(r["reply"]["shell"] or "") for r in run.main_requests())
        ev["accept_driven_without_delivery"] = False
        if not ran_accept:
            # The delivery failed; still measure the accept path from this harness's shell.
            ev["accept_driven_without_delivery"] = True
            start = len(run.main_requests())
            run.prompt("SCENARIO-HANDOFF accept-now")
            run.wait_idle(6, 60)
            ran_accept = any("handoff accept" in str(r["reply"]["shell"] or "")
                             for r in run.main_requests()[start:])
        out = next((r["last_text"] for r in run.main_requests()
                    if r["last_kind"] == "tool" and ("accepted=" in r["last_text"]
                                                      or "handoff" in r["last_text"])), None)
        ev["accept_tool_output"] = (out or "")[-600:]
        ev["pending_after"] = pending_state(run, self.ref)
        acceptance = (ev["pending_after"] or {}).get("acceptance") or {}
        accepted = acceptance.get("read_and_accepted") is True
        ev["accepted_recorded"] = accepted
        ev["reply_scheduled"] = bool(acceptance.get("reply_ref"))
        run.note(f"carrier={ev['handoff_carrier']} accepted={accepted} "
                 f"out={(out or '-').strip()[:90]}")
        if ev["handoff_carrier"] == "not-delivered":
            return "gap" if not accepted else "fail"
        return "pass" if accepted and ran_accept else "fail"


SCENARIOS = [ReportArrivalScenario, QuestionAnswerScenario, HandoffAcceptScenario]
