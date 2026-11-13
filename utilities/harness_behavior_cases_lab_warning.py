#!/usr/bin/env python3
"""Timing probe for ``harness_behavior_scenarios.py``: can a warning reach the agent before the person answers?

``lab-warning-timing``
    BC-LAB-REPORT-LEGIBILITY requirement 6 asks to warn about an internal name *before* a reply or
    a choice list is sent.  Whether any native hook can do that is a fact about each harness, so it
    is measured, not assumed.  On the real Claude, Codex and OpenCode TUIs (private tmux, loopback
    model, throwaway HOME; no account, no operator session) the model asks one native question and
    the person answers in the TUI.  A probe the scenario installs in the throwaway HOME records:

    * when the earliest hook for the question (Claude ``PreToolUse[AskUserQuestion]``, Codex
      ``PreToolUse[request_user_input]``, OpenCode ``tool.execute.before`` for ``question``) ran,
    * when the form was first seen on the screen (polled every 50 ms),
    * when the final reply text first appeared, against the harness's end-of-reply carriers
      (Claude/Codex ``Stop``, OpenCode ``experimental.text.complete`` and ``session.idle``),
    * whether the probe's own string (``LABWARN-PROBE``, added as ``additionalContext`` by the
      question hook) reached the model, and in which request relative to the request that carried
      the person's answer.

    Oracle 1: hook time < screen time (before the person sees it).  Oracle 2: the probe string is in
    a model request *earlier* than the one carrying the answer (before the agent could act on it).
    The row's verdict is ``pass`` when the measurement completed; the table is in ``evidence``.

The probe only appends to a file under the fixture root and, for the question hook, returns an
``additionalContext``; it never blocks, rewrites, or answers.  Nothing in the shipped hooks changes.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_cases_b as qb  # noqa: E402
from harness_behavior_scenarios import wait_for  # noqa: E402

PROBE_STRING = "LABWARN-PROBE"

PROBE_PY = r'''
import json, os, sys, time
event, harness, marks = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.loads(sys.stdin.read() or "{}")
except ValueError:
    data = {}
with open(marks, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"event": event, "harness": harness, "t": time.time(),
                             "tool": data.get("tool_name"), "hook": data.get("hook_event_name")}) + "\n")
if event.startswith("pre"):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "additionalContext": "LABWARN-PROBE " + event}}))
'''

OPENCODE_PROBE = r'''
import fs from "node:fs"
const mark = (event, extra = {}) => {
  try {
    fs.appendFileSync(__MARKS__, JSON.stringify({ event, harness: "opencode", t: Date.now() / 1000, ...extra }) + "\n")
  } catch (_) {}
}
// OpenCode 2 loads a plugin by `{ id, setup }` and gives it `ctx.<area>.hook(name, handler)`, the same
// surface `hearting-guards.js` uses.  `tool.execute.before` is the question's earliest carrier;
// `session.http.response` is the nearest to "the model's reply reached the host".  The 1.x
// `experimental.text.complete` and `session.idle` names are not part of this surface.
export default {
  id: "labwarn.probe",
  setup: async (ctx) => {
    await ctx.tool.hook("execute.before", async (event) => { mark("pre-tool", { tool: event.tool }) })
    await ctx.session.hook("http.response", async () => { mark("http-response") })
  },
}
'''


def _own(path: Path) -> str:
    """The file's text, made a real file in the fixture (never write through a symlink into the source tree)."""
    text = path.read_text(encoding="utf-8")
    if path.is_symlink():
        path.unlink()
        path.write_text(text, encoding="utf-8")
    return text


def _command(probe: Path, event: str, harness: str, marks: Path) -> dict:
    # The marks path is in the command itself: a hook's environment is the harness's to choose.
    return {"type": "command", "command": f"python3 {probe} {event} {harness} {marks}", "timeout": 10}


class LabWarningTimingScenario(qb.QuestionAnswerScenario):
    name = "lab-warning-timing"
    summary = ("Measures, per harness, whether a hook can reach the agent with a warning before the person "
               "answers a native question or sees the reply (probe only; blocks and changes nothing).")

    def prepare(self, run):
        super().prepare(run)
        root = run.home.root
        marks = root / "labwarn-marks.jsonl"
        marks.write_text("", encoding="utf-8")
        run.home.env["LABWARN_MARKS"] = str(marks)
        probe = root / "labwarn-probe.py"
        probe.write_text(PROBE_PY, encoding="utf-8")
        run.evidence["probe_marks"] = str(marks)
        if run.harness == "claude":
            path = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            settings = json.loads(_own(path))
            hooks = settings.setdefault("hooks", {})
            hooks.setdefault("PreToolUse", []).append(
                {"matcher": "AskUserQuestion", "hooks": [_command(probe, "pre-question", "claude", marks)]})
            hooks.setdefault("Stop", []).append({"hooks": [_command(probe, "stop", "claude", marks)]})
            path.write_text(json.dumps(settings, indent=1), encoding="utf-8")
        elif run.harness == "codex":
            path = Path(run.home.env["CODEX_HOME"]) / "hooks.json"
            config = json.loads(_own(path))
            hooks = config.setdefault("hooks", {})
            hooks.setdefault("PreToolUse", []).append(
                {"matcher": "request_user_input", "hooks": [_command(probe, "pre-question", "codex", marks)]})
            hooks.setdefault("Stop", []).append({"hooks": [_command(probe, "stop", "codex", marks)]})
            path.write_text(json.dumps(config, indent=1), encoding="utf-8")
            # The probe makes hooks.json a file the managed trust record no longer vouches for, so Codex
            # asks at start; the scenario answers "Trust all and continue" (the default is "without trusting").
            def dismiss(tui, original=run._dismiss_startup):
                tui.settle()
                if "Trust all and continue" in tui.screen():
                    run.evidence["codex_dialog_before"] = tui.screen()[-700:]
                    tui.key("2")
                    time.sleep(1)
                    if "Trust all and continue" in tui.screen():
                        tui.key("Enter")
                    run.evidence["codex_hook_trust_dialog"] = "trust-all"
                    time.sleep(2)
                    run.evidence["codex_dialog_after"] = tui.screen()[-500:]
                else:
                    original(tui)
            run._dismiss_startup = dismiss
        else:
            plugins = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "plugins"
            plugins.mkdir(parents=True, exist_ok=True)
            (plugins / "labwarn-probe.js").write_text(OPENCODE_PROBE.replace("__MARKS__", json.dumps(str(marks))), encoding="utf-8")

    @staticmethod
    def _marks(run) -> list:
        rows = []
        try:
            for line in Path(run.evidence["probe_marks"]).read_text(encoding="utf-8").splitlines():
                with_row = json.loads(line) if line.strip() else None
                if with_row:
                    rows.append(with_row)
        except (OSError, ValueError):
            pass
        return rows

    @staticmethod
    def _first_seen(run, needle: str, timeout: float):
        """The wall-clock second `needle` is first on the screen (polled every 50 ms), or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in run.tui.screen():
                return time.time()
            time.sleep(0.05)
        return None

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-QUESTION please ask me")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        seen_form = self._first_seen(run, qb.OPTION_A, 40)
        ev["form_seen_at"] = seen_form
        ev["screen_question"] = run.tui.screen()[-1200:]
        if seen_form is None:
            run.note("question form never rendered")
            return "inconclusive"
        time.sleep(1)
        marks = self._marks(run)
        asks = [m for m in marks if m["event"] == "pre-question" or (m["event"] == "pre-tool" and m.get("tool") == "question")]
        pre = [m["t"] for m in asks]
        ev["pre_question_marks"] = asks
        before = len(run.main_requests())
        watch = {"text_seen": None, "stop": False}

        def watcher():
            while not watch["stop"] and watch["text_seen"] is None:
                if "answer noted" in run.tui.screen():
                    watch["text_seen"] = time.time()
                time.sleep(0.05)
        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        sent = []
        for keys in (["Enter"], ["Enter"], ["Enter"], ["Tab", "Enter"]):
            run.tui.key(*keys)
            sent.append("+".join(keys))
            if wait_for(lambda: len(run.main_requests()) > before, 8):
                break
        ev["answer_keys"] = sent
        answered_at = time.time()
        run.wait_idle(5, 60)
        time.sleep(1)
        watch["stop"] = True
        thread.join(2)
        text_seen = watch["text_seen"]
        marks = self._marks(run)
        ev["reply_text_seen_at"] = text_seen
        ev["all_marks"] = marks
        end_marks = {m["event"]: m["t"] for m in marks if m["event"] in ("stop", "http-response", "text-complete", "session-idle")}
        requests = run.main_requests()
        answer_index = next((r["main_index"] for r in requests[before:] if qb.OPTION_A in r.get("last_text", "")), None)
        probe_index = next((r["main_index"] for r in requests if PROBE_STRING in r["text"]), None)
        ev["answer_request"], ev["probe_first_request"] = answer_index, probe_index
        table = {
            "question_hook_fired": bool(pre),
            "question_hook_before_screen": bool(pre) and min(pre) <= seen_form,
            "question_hook_lead_seconds": round(seen_form - min(pre), 3) if pre else None,
            "probe_reached_model": probe_index is not None,
            "probe_before_answer_request": (probe_index is not None and answer_index is not None and probe_index < answer_index),
            "probe_request_vs_answer_request": (None if probe_index is None or answer_index is None
                                                else "before" if probe_index < answer_index
                                                else "same" if probe_index == answer_index else "after"),
            "reply_text_seen_at": text_seen, "answered_at": answered_at,
            "end_of_reply_carriers_minus_text_seen": {name: (None if text_seen is None else round(t - text_seen, 3))
                                      for name, t in end_marks.items()},
        }
        ev["timing"] = table
        run.note(json.dumps(table, ensure_ascii=False))
        return "pass"


SCENARIOS = (LabWarningTimingScenario,)
