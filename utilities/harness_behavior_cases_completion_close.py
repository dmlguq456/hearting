#!/usr/bin/env python3
"""Isolated completion-close transport probes with screen-derived fact checks."""
from __future__ import annotations

from pathlib import Path

from harness_behavior_cases_a import CompletionReceiptScenario, _shell

EVENT_TOKENS = {
    "failure": "HB-FAIL-CAUSE",
    "partial": "HB-UNRUN-CHECK",
    "decision": "HB-DECISION-A-OR-B",
}
RECORD_TOKEN = "HB-RECORD-PATH"
OLD_REQUIRED_SECTIONS = (
    "Changes/why; before/after; checks run/reused/skipped/unrun; "
    "commit/PR/deployment/serving; limits."
)
OLD_DIRECTIVE = "show the runtime summary below to the person first, as written"


def synthetic_sealed_summary(event: str) -> str:
    """The exact four-line report written by the registered fixture child."""
    import work_intent
    token = EVENT_TOKENS.get(event, "HB-SUCCESS")
    return (f"{work_intent.summary_heads()[0]}\nResult: simulated {event} completion.\n"
            f"{token}\nRecord: {RECORD_TOKEN}")


def derived_old_minimum(event: str) -> dict[str, int | str]:
    summary = synthetic_sealed_summary(event)
    return {"measurement": "derived", "summary_chars": len(summary),
            "required_sections_chars": len(OLD_REQUIRED_SECTIONS),
            "minimum_user_chars": len(summary) + len(OLD_REQUIRED_SECTIONS)}


def event_tokens_in_carrier(carrier: str) -> list[str]:
    """Return only sentinels actually present in the delivered carrier."""
    return [token for token in (*EVENT_TOKENS.values(), RECORD_TOKEN) if token in carrier]


def scripted_reply_from_carrier(carrier: str) -> tuple[str, str]:
    """Build deterministic transport text from the actual request carrier."""
    tokens = event_tokens_in_carrier(carrier)
    if OLD_DIRECTIVE in carrier:
        summary = next((line for line in carrier.splitlines()
                        if line.startswith("[Completion summary]") or line.startswith("[완료 요약]")), "")
        # The fixture summary has a fixed four-line shape; retain it byte-for-byte.
        start = next((i for i, line in enumerate(carrier.splitlines())
                      if line.startswith("[Completion summary]") or line.startswith("[완료 요약]")), None)
        lines = carrier.splitlines()
        summary = "\n".join(lines[start:start + 4]) if start is not None else summary
        return (summary + "\n" + OLD_REQUIRED_SECTIONS, "derived-old-directive")
    result = "Result: completion received."
    if tokens:
        result += " " + " ".join(tokens)
    return result, "scripted-transport"


def extract_event_facts(reply_text: str, event: str) -> dict[str, bool]:
    """Measure required facts exclusively from the captured display reply."""
    expected = EVENT_TOKENS.get(event)
    return {
        "failure": event != "failure" or (expected in reply_text if expected else False),
        "unfinished": event != "partial" or (expected in reply_text if expected else False),
        "decision": event != "decision" or (expected in reply_text if expected else False),
        "record_pointer": RECORD_TOKEN in reply_text,
    }


def close_passes(reply_text: str, event: str) -> bool:
    facts = extract_event_facts(reply_text, event)
    return bool(reply_text) and all(facts.values())


class CompletionCloseScenario(CompletionReceiptScenario):
    event = "success"
    event_context = "The work completed successfully; nothing remains."
    summary = "Capture completion carrier and the private TUI's user-facing close."

    def __init__(self):
        super().__init__()
        self.PROMPT = ("SCENARIO-RECEIPT start the registered child and end your turn. "
                       f"Recorded outcome: {self.event_context}")
        self.PROBE = f"SCENARIO-CLOSE-{self.event.upper()} anything new?"

    def prepare(self, run):
        from harness_behavior_cases_a import _install_helper
        _install_helper(run, self.attempt, event=self.event)

    def script(self, run):
        def answer(record, earlier):
            carrier_request = next((r for r in reversed([*earlier, record])
                                    if self.attempt in r.get("text", "")
                                    and "First close:" in r.get("text", "")), None)
            if carrier_request:
                carrier_text = carrier_request["text"]
                shell_already_ran = any(self.NEXT in (r.get("reply", {}).get("shell") or "")
                                        for r in earlier)
                if not shell_already_ran:
                    return Reply(shell=f"echo {self.NEXT}-$((20+22))")
                if record.get("last_kind") == "tool":
                    close, _mode = scripted_reply_from_carrier(carrier_text)
                    return Reply(close)
            last = record.get("last_text", "")
            if "SCENARIO-RECEIPT start" in last and not any(
                    r.get("reply", {}).get("shell") for r in earlier):
                return Reply(shell=_shell(run, *self.register_args()))
            if self.NEXT in last:
                return Reply("Next action finished.")
            return Reply("Nothing new.")
        return answer

    def drive(self, run):
        verdict = super().drive(run)
        requests = run.main_requests()
        request = next((r for r in reversed(requests)
                        if self.attempt in r.get("text", "")
                        and "First close:" in r.get("text", "")), None)
        carrier = request.get("text", "") if request else ""
        carrier_file = Path(run.output) / run.harness / f"completion-close-{self.event}.carrier.txt"
        carrier_file.parent.mkdir(parents=True, exist_ok=True)
        carrier_file.write_text(carrier, encoding="utf-8")

        replies = [r.get("reply", {}).get("text", "") for r in requests
                   if isinstance(r.get("reply"), dict)]
        model_reply = next((reply for reply in reversed(replies) if RECORD_TOKEN in reply), "")
        screen = run.tui.screen() if getattr(run, "tui", None) is not None else ""
        displayed_reply = model_reply if model_reply and model_reply in screen else ""
        screen_remainder = screen.replace(displayed_reply, "", 1) if displayed_reply else screen
        screen_file = Path(run.output) / run.harness / f"completion-close-{self.event}.screen-nonreply.txt"
        screen_file.write_text(screen_remainder, encoding="utf-8")
        expected_summary = ((run.evidence.get("complete") or {}).get("sealed_summary") or "")
        summary_carried = bool(expected_summary) and expected_summary in carrier
        event_token = EVENT_TOKENS.get(self.event)
        required_carrier_tokens = [RECORD_TOKEN] + ([event_token] if event_token else [])
        sentinels_carried = bool(expected_summary) and all(
            token in expected_summary and token in carrier for token in required_carrier_tokens)
        old_directive_present = OLD_DIRECTIVE in carrier
        contract_present = (old_directive_present or
                            ("one or two sentences" in carrier and not old_directive_present))
        facts = extract_event_facts(displayed_reply, self.event)
        mode = scripted_reply_from_carrier(carrier)[1] if carrier else "unavailable"
        evidence = {
            "measurement": "scripted-transport",
            "event": self.event,
            "carrier_path": str(carrier_file),
            "carrier_chars": len(carrier),
            "carrier_lines": len(carrier.splitlines()),
            "summary_carried_unchanged": summary_carried,
            "summary_chars": len(expected_summary),
            "required_sentinels_carried_from_summary": sentinels_carried,
            "carrier_contract_present": contract_present,
            "old_show_as_written_directive_present": old_directive_present,
            "carrier_event_tokens": event_tokens_in_carrier(carrier),
            "reply_mode": mode,
            "screen_reply": displayed_reply,
            "screen_reply_chars": len(displayed_reply),
            "screen_reply_lines": len(displayed_reply.splitlines()),
            "screen_reply_captured": bool(displayed_reply),
            "screen_non_reply_path": str(screen_file),
            "hook_plugin_screen_capture": screen_remainder,
            "hook_plugin_screen_chars": len(screen_remainder),
            "screen_non_reply_chars": len(screen_remainder),
            "screen_non_reply_lines": len(screen_remainder.splitlines()),
            "screen_non_reply_text": screen_remainder,
            "preserved_facts": facts,
        }
        if OLD_DIRECTIVE in carrier:
            evidence["derived_old_minimum_user_chars"] = len(expected_summary) + len(OLD_REQUIRED_SECTIONS)
            evidence["derived_old_required_sections"] = OLD_REQUIRED_SECTIONS
        run.evidence["completion_close"] = evidence
        if verdict == "pass" and not (summary_carried and sentinels_carried and contract_present
                                       and close_passes(displayed_reply, self.event)):
            verdict = "fail"
            run.note("carrier summary/contract or captured screen reply omitted a required fact")
        return verdict


class CompletionCloseSuccess(CompletionCloseScenario):
    name = "completion-close-success"
    event = "success"


class CompletionCloseFailure(CompletionCloseScenario):
    name = "completion-close-failure"
    event = "failure"
    event_context = "The operation failed with a simulated error; the cause remains unresolved."


class CompletionClosePartial(CompletionCloseScenario):
    name = "completion-close-partial"
    event = "partial"
    event_context = "Two checks passed; one could not run, so work is partial."


class CompletionCloseDecision(CompletionCloseScenario):
    name = "completion-close-decision"
    event = "decision"
    event_context = "The work is complete; the person must decide whether to proceed with option A or B."


SCENARIOS = [CompletionCloseSuccess, CompletionCloseFailure,
             CompletionClosePartial, CompletionCloseDecision]
