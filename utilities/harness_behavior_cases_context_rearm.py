#!/usr/bin/env python3
"""Behaviour scenarios: the 500K/700K context notice after compaction and on a wake.

Loaded by ``harness_behavior_scenarios.py`` (``SCENARIOS`` below).  Each runs on
the real TUI in the base harness's throwaway HOME with the loopback model; the
fixture model's reply to each prompt reports the input size the scenario sets,
so the notice at a prompt reads the size the previous reply reported.

``context-alert-rearm``
    A 1,000,000-token fixture model with native automatic compaction off.  The
    replies report 1K, 720K, 720K, 720K; the person then types the native
    ``/compact`` (the command session-tidy books for the same session) and the
    replies report 30K, 520K, 720K, 720K, 720K.  Pass: the urgent line once at
    the jump across both thresholds, silence while the session stays there,
    then after the compaction the recommendation line at 520K and the urgent
    line at 720K, once each.

``context-alert-rearm-auto``
    The same with the harness's own automatic compaction instead of a typed
    ``/compact``: the replies report 1K, 720K, 999K (past every runtime's own
    compaction point on the 1,000,000-token window), 30K, 520K, 720K, 1K.
    Pass: urgent once, then after the runtime's compaction the recommendation
    and the urgent line once each.

``context-alert-wake``
    The session registers a child (the ``completion-receipt`` setup) with small
    replies, so no runtime input before the wake has a notice due; one typed
    turn then reports 720K and ends.  The child completes and the runtime wakes
    the session without a typed prompt.  Pass: the urgent line reaches the
    model within the woken turn (its wake request or a later request of that
    turn); where it arrived is recorded.

An alert is new in a request when that request carries more copies of the
line than the session's previous conversation request (not a compaction or a
helper's title request).  Codex takes its 1,000,000-token window from a
generic fixture model catalog (``CODEX_MODEL``).  ``--replay <requests.json>``
re-judges a stored rearm run with the same reader.
Synthetic data: the fixture HOME's model settings and, for the wake case, the
``harness_behavior_cases_a`` registry row and ``sleep`` child.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
import harness_behavior_cases_a as ca  # noqa: E402
from harness_behavior_cases_tidy_compact import _is_compaction, summary_reply  # noqa: E402

MARK = "SCENARIO-CTXR"
ALERT = re.compile(r"입력 문맥 약 (\d+)K[^\n\"\\]*")


def alerts(text: str) -> Counter:
    """Each notice line as ``<size>K:urgent|recommend``."""
    found = Counter()
    for match in ALERT.finditer(text or ""):
        kind = "recommend" if "저장 권장" in match.group(0) else "urgent"
        found[f"{match.group(1)}K:{kind}"] += 1
    return found


def prompt_index(record) -> int | None:
    found = re.findall(MARK + r" (\d+)", record.get("text") or "")
    return int(found[-1]) if found else None


def new_alerts(record, previous) -> list[str]:
    added = alerts(record["text"])
    if previous is not None:
        added -= alerts(previous["text"])
    return sorted(added.elements())


def conversation(record) -> bool:
    """A request of the session's own conversation: not its compaction request
    and not a helper's title request (Hearting's titler reuses the same model)."""
    return bool(record.get("main")) and not _is_compaction(record) \
        and "Do not answer the request" not in (record.get("last_text") or "")


def alerts_by_prompt(requests: list[dict], count: int) -> list:
    """The notice lines each prompt added, judged on the stored requests.

    A prompt's window runs from the conversation request before its first one
    to its last one; a compaction request inside it counts too (a runtime that
    compacts after the prompt hook may carry the line only there)."""
    convo = [i for i, r in enumerate(requests) if conversation(r)]
    out = []
    for index in range(count):
        rows = [i for i in convo if prompt_index(requests[i]) == index]
        if not rows:
            out.append("missing")
            continue
        prior = [i for i in convo if i < rows[0]]
        previous = requests[prior[-1]] if prior else None
        start = prior[-1] + 1 if prior else 0
        found = []
        for position in range(start, rows[-1] + 1):
            record = requests[position]
            if not (conversation(record) or _is_compaction(record)):
                continue
            for line in new_alerts(record, previous):
                if line not in found:
                    found.append(line)
        out.append(found[0] if len(found) == 1 else (found or None))
    return out


# Codex 0.163 takes ``model_context_window`` only up to the model's own
# ``max_context_window``; a model missing from its catalog gets the
# 272,000-token default.  A generic schema-only catalog entry (no instructions,
# no account identity), named by ``model_catalog_json``, lets the fixture model
# hold a 1,000,000-token window.
CODEX_MODEL = {
    "slug": "hearting-fixture", "display_name": "hearting-fixture", "description": "fixture",
    "default_reasoning_level": "low",
    "supported_reasoning_levels": [{"effort": "low", "description": "fixture"}],
    "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True, "priority": 3,
    "additional_speed_tiers": [], "service_tiers": [],
    "available_access_programs": {"cyber": []},
    "availability_nux": None, "upgrade": None,
    "model_messages": {"persistent_instructions": "fixture", "instructions_template": "fixture",
                       "approvals": {"on_request": None, "on_request_auto_review": "fixture",
                                     "never": None, "unless_trusted": None},
                       "collaboration_modes": {"default": "fixture", "plan": None},
                       "auto_review": {"policy": None, "policy_template": None,
                                       "rejection_instructions": "fixture",
                                       "timeout_instructions": None},
                       "permissions": None,
                       "multi_agent": {"role": {"root": "fixture", "subagent": "fixture"},
                                       "mode": None},
                       "token_budget": {"enabled": False, "use_history_notes_extension": False,
                                        "reminder_threshold_tokens": 6144,
                                        "reminder_message_template": "fixture",
                                        "guidance_message": "fixture",
                                        "auto_compact_fallback_prompt": "fixture",
                                        "auto_compact_fallback_buffer_tokens": 16384},
                       "guardian_v2": {"classifier_instructions": "fixture"},
                       "confirmation_policies": {"browser_use": "fixture",
                                                 "computer_use": "fixture"}},
    "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False,
    "include_apps_usage_instructions": False, "default_reasoning_summary": "none",
    "support_verbosity": True, "default_verbosity": "low", "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text_and_image", "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supports_image_detail_original": True, "comp_hash": "3000",
    "effective_context_window_percent": 95, "experimental_supported_tools": [],
    "input_modalities": ["text", "image"], "supports_search_tool": False,
    "supports_experimental_context": False, "use_responses_lite": False,
    "supports_reasoning_effort_updates": True, "node_repl_auto_review_required": True,
    "node_repl_disabled": False, "tool_mode": "direct", "multi_agent_version": "v1",
    "multi_agent_reasoning_effort": "low",
}


def configure_window(run, window: int, auto: bool) -> None:
    """The fixture model's window; native automatic compaction off unless ``auto``."""
    env = run.home.env
    if run.harness == "codex":
        home = Path(env["CODEX_HOME"])
        path = home / "config.toml"
        text = path.read_text(encoding="utf-8")
        limit = int(window * 0.9) if auto else 10_000_000
        path.write_text(f"model_context_window = {window}\n"
                        f"model_auto_compact_token_limit = {limit}\n" + text,
                        encoding="utf-8")
        catalog = home / "fixture-model-catalog.json"
        catalog.write_text(json.dumps({"models": [{**CODEX_MODEL, "context_window": window,
                                                    "max_context_window": window}]}),
                           encoding="utf-8")
        path.write_text(f"model_catalog_json = {json.dumps(str(catalog))}\n"
                        + path.read_text(encoding="utf-8"), encoding="utf-8")
        run.evidence["codex_model_catalog_window"] = window
    elif run.harness == "claude":
        # Claude 2.1 does not enforce a window for an unknown model; this names it.
        if auto:
            env.pop("DISABLE_AUTO_COMPACT", None)
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
        else:
            env["DISABLE_AUTO_COMPACT"] = "1"
    elif run.harness == "opencode":
        path = Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        for model in config["provider"]["fixture"]["models"].values():
            model["limit"]["context"] = window
        path.write_text(json.dumps(config, indent=1), encoding="utf-8")


class ContextAlertRearm(Scenario):
    name = "context-alert-rearm"
    summary = ("500K/700K notices speak once, stay quiet at the same height, and speak "
               "once more after the same session's /compact and a new crossing.")
    usage = (1_000, 720_000, 720_000, 720_000, 30_000, 520_000, 720_000, 720_000, 720_000)
    compact_before = 4     # the person types /compact before this prompt
    expected = (None, None, "720K:urgent", None, None, None, "520K:recommend", "720K:urgent", None)
    window, auto = 1_000_000, False

    def prepare(self, run):
        configure_window(run, self.window, self.auto)

    def script(self, run):
        def answer(record, earlier):
            if _is_compaction(record):
                return summary_reply(record, "ctxr")
            index = prompt_index(record) or 0
            return Reply(f"OK {index}", input_tokens=self.usage[min(index, len(self.usage) - 1)])
        return answer

    def side_script(self, run):
        def side(record):
            return summary_reply(record, "ctxr-side") if _is_compaction(record) else None
        return side

    def compact(self, run, index):
        if index != self.compact_before:
            return
        count = len(run.fixture.requests)
        run.tui.type_text("/compact")
        seen = wait_for(lambda: any(_is_compaction(r) for r in run.fixture.requests[count:]), 90, 1)
        run.wait_idle(8, 90)
        run.evidence.setdefault("typed_compactions", []).append(bool(seen))
        time.sleep(1)

    def drive(self, run):
        compactions = []
        for index in range(len(self.usage)):
            self.compact(run, index)
            before = len(run.fixture.requests)
            run.prompt(f"{MARK} {index}")
            if not wait_for(lambda: any(prompt_index(r) == index and conversation(r)
                                        for r in run.fixture.requests[before:]), 90, 1):
                run.note(f"prompt {index}: no model request")
                return "error"
            run.wait_idle(quiet=5, timeout=90)
            compactions.append(sum(1 for r in run.fixture.requests[before:] if _is_compaction(r)))
            time.sleep(1)
        seen = alerts_by_prompt(list(run.fixture.requests), len(self.usage))
        run.evidence["alert_by_prompt"] = seen
        run.evidence["expected_by_prompt"] = list(self.expected)
        run.evidence["compaction_requests_by_prompt"] = compactions
        run.evidence["usage_by_prompt"] = list(self.usage)
        if list(self.expected) == seen:
            return "pass"
        run.note(f"alerts {seen} != expected {list(self.expected)}")
        return "fail"


class ContextAlertRearmAuto(ContextAlertRearm):
    name = "context-alert-rearm-auto"
    summary = ("The same crossings with the harness's own automatic compaction: each "
               "crossing after a compaction speaks once more.")
    # 999K passes every runtime's automatic-compaction point on the 1,000,000-token
    # window (Codex: the 900K limit below; Claude: its window less the output
    # reserve; OpenCode: context less the 8,000-token output limit).
    usage = (1_000, 720_000, 999_000, 30_000, 520_000, 720_000, 1_000)
    compact_before = None
    expected = (None, None, "720K:urgent", None, None, "520K:recommend", "720K:urgent")
    window, auto = 1_000_000, True


class ContextAlertWake(ca.CompletionReceiptScenario):
    name = "context-alert-wake"
    summary = ("A main session above 700K that is woken only by a runtime completion "
               "receives the urgent notice within the woken turn.")
    PROMPT = f"{MARK}-WAKE start the registered child and end your turn"
    HOLD = f"{MARK}-WAKE-HIGH hold until the child completes"
    HIGH = 720_000

    def __init__(self):
        super().__init__()
        self.completed = False

    def prepare(self, run):
        configure_window(run, 1_000_000, False)
        super().prepare(run)

    def script(self, run):
        # Every reply before the completion is small except the typed HOLD turn's,
        # so no runtime input before the wake (a start receipt, say) has a notice due.
        def answer(record, earlier):
            if _is_compaction(record):
                return summary_reply(record, "ctxw")
            last, kind = record["last_text"], record["last_kind"]
            done_next = any(self.NEXT in (r["reply"].get("shell") or "") for r in earlier)
            if kind == "tool":
                if self.NEXT in last:
                    return Reply("Next action finished.", input_tokens=self.HIGH)
                return Reply("The child is running; I will continue when it completes.",
                             input_tokens=1_000)
            if self.completed and self.attempt in record["text"] and not done_next:
                return Reply(shell=f"echo {self.NEXT}-$((20+22))", input_tokens=self.HIGH)
            if self.HOLD in last:
                return Reply("Holding until the completion arrives.", input_tokens=self.HIGH)
            if f"{MARK}-WAKE start" in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=ca._shell(run, *self.register_args()), input_tokens=1_000)
            return Reply("Nothing new.", input_tokens=self.HIGH if self.completed else 1_000)
        return answer

    def drive(self, run):
        run.prompt(self.PROMPT)
        run.wait_idle(8, 120)
        if not any("HB-REGISTERED" in r["text"] for r in run.main_requests()):
            run.note("the register shell call never reached the model")
            return "inconclusive"
        time.sleep(4)
        run.wait_idle(6, 60)
        run.prompt(self.HOLD)
        run.wait_idle(8, 90)
        requests = run.main_requests()
        convo = [r for r in requests if conversation(r)]
        early = [line for i, r in enumerate(convo) for line in
                 new_alerts(r, convo[i - 1] if i else None)]
        run.evidence["alerts_before_wake"] = early
        time.sleep(2)
        before = len(run.main_requests())
        self.completed = True
        run.evidence["complete"] = ca._helper(run, "complete")
        woke = wait_for(lambda: [r for r in ca._carrying(run.main_requests(), self.attempt, before)
                                 if conversation(r)], 120, 1)
        run.wait_idle(8, 60)
        requests = run.main_requests()
        if not woke:
            run.note("no wake without a prompt; the notice could not be judged on a wake")
            return "inconclusive"
        first = woke[0]["main_index"]
        prior = [r for r in requests[:first] if conversation(r)]
        previous = prior[-1] if prior else None
        where = None
        for record in [r for r in requests[first:] if conversation(r) or _is_compaction(r)]:
            if "720K:urgent" in new_alerts(record, previous):
                where = "wake-request" if record["main_index"] == first else (
                    f"woken-turn request +{record['main_index'] - first} ({record['last_kind']})")
                break
        run.evidence["wake_request_index"] = first
        run.evidence["wake_request_kind"] = woke[0]["last_kind"]
        run.evidence["alert_in_woken_turn"] = where
        run.evidence["woken_turn_requests"] = len(requests) - first
        if early:
            run.note(f"a notice arrived before the wake: {early}")
            return "inconclusive"
        if where:
            run.note(f"urgent notice reached the woken turn at {where}")
            return "pass"
        run.note("urgent notice did not reach the woken turn")
        return "fail"


SCENARIOS = (ContextAlertRearm, ContextAlertRearmAuto, ContextAlertWake)


def replay(paths) -> int:
    """Re-judge stored ``requests.json`` files: ``--replay <dir|requests.json>...``."""
    scenarios = {cls.name: cls for cls in SCENARIOS}
    for raw in paths:
        path = Path(raw)
        path = path / "requests.json" if path.is_dir() else path
        name = next((n for n in sorted(scenarios, key=len, reverse=True)
                     if path.parent.name.endswith(n)), "")
        cls = scenarios.get(name)
        requests = json.loads(path.read_text(encoding="utf-8"))
        if cls is None or not hasattr(cls, "usage"):
            print(json.dumps({"path": str(path), "verdict": "not-replayable"}))
            continue
        seen = alerts_by_prompt(requests, len(cls.usage))
        print(json.dumps({"path": str(path), "scenario": name,
                          "verdict": "pass" if seen == list(cls.expected) else "fail",
                          "alert_by_prompt": seen}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--replay":
        raise SystemExit(replay(sys.argv[2:]))
    print("usage: harness_behavior_cases_context_rearm.py --replay <requests.json|dir>...")
