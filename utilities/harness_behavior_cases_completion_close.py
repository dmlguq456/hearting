#!/usr/bin/env python3
"""Transport-only completion-close probes; model compliance is not measured here."""
from __future__ import annotations

from pathlib import Path

from harness_behavior_cases_a import CompletionReceiptScenario, _shell
from harness_behavior_scenarios import Reply

EVENT_TOKENS = {
    "success": "HB-SUCCESS",
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
CURRENT_DIRECTIVE = "give the result in one or two sentences"
LOOPBACK_REPLY = "Completion notice received."


def synthetic_sealed_summary(event: str) -> str:
    """The exact four-line report written by the registered fixture child."""
    import work_intent
    token = EVENT_TOKENS[event]
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


def completion_carrier(record: dict, attempt: str) -> str:
    """The newest request item carrying this attempt's completion instruction."""
    text = record.get("last_text", "")
    if isinstance(text, str) and attempt in text and "First close:" in text:
        return text
    return ""


def observed_directive(carrier: str) -> str:
    """Name the first-close instruction actually present in the carrier."""
    old = OLD_DIRECTIVE in carrier
    current = CURRENT_DIRECTIVE in carrier
    if old and current:
        return "old-and-current"
    if old:
        return "old"
    if current:
        return "current"
    return "none"


def split_screen_reply(screen: str, reply: str) -> str:
    """Remove the captured model line once, leaving Hearting display text."""
    return screen.replace(reply, "", 1) if reply else screen


def carrier_facts(carrier: str, summary: str, event: str) -> dict:
    """Report byte and sentinel facts observed in the delivered request."""
    expected = EVENT_TOKENS[event]
    return {
        "summary_byte_identical": bool(summary) and summary in carrier,
        "expected_sentinel_present": expected in carrier and expected in summary,
        "record_sentinel_present": RECORD_TOKEN in carrier and RECORD_TOKEN in summary,
        "unexpected_event_sentinels_absent": all(
            token not in carrier for name, token in EVENT_TOKENS.items() if name != event),
        "sentinels_observed": event_tokens_in_carrier(carrier),
        "directive_observed": observed_directive(carrier),
    }


class CompletionCloseScenario(CompletionReceiptScenario):
    event = "success"
    event_context = "The work completed successfully; nothing remains."
    summary = "Capture delivered completion carrier and non-reply screen text."

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
                                    if completion_carrier(r, self.attempt)), None)
            if carrier_request:
                shell_already_ran = any(self.NEXT in (r.get("reply", {}).get("shell") or "")
                                        for r in earlier)
                if not shell_already_ran:
                    return Reply(shell=f"echo {self.NEXT}-$((20+22))")
                if record.get("last_kind") == "tool":
                    return Reply(LOOPBACK_REPLY)
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
                        if completion_carrier(r, self.attempt)), None)
        carrier = completion_carrier(request, self.attempt) if request else ""
        carrier_file = Path(run.output) / run.harness / f"completion-close-{self.event}.carrier.txt"
        carrier_file.parent.mkdir(parents=True, exist_ok=True)
        carrier_file.write_text(carrier, encoding="utf-8")

        screen = run.tui.screen() if getattr(run, "tui", None) is not None else ""
        reply_was_sent = any(
            isinstance(r.get("reply"), dict) and r["reply"].get("text") == LOOPBACK_REPLY
            for r in requests)
        displayed_reply = LOOPBACK_REPLY if reply_was_sent and LOOPBACK_REPLY in screen else ""
        screen_remainder = split_screen_reply(screen, displayed_reply)
        screen_file = Path(run.output) / run.harness / f"completion-close-{self.event}.screen-nonreply.txt"
        screen_file.write_text(screen_remainder, encoding="utf-8")
        expected_summary = ((run.evidence.get("complete") or {}).get("sealed_summary") or "")
        facts = carrier_facts(carrier, expected_summary, self.event) if expected_summary else {}
        import harness_behavior_scenarios as runner
        baseline_source = Path(runner.SOURCE).resolve() != Path(runner.ROOT).resolve()
        directive = facts.get("directive_observed", "none")
        directive_valid = directive != "none" if baseline_source else directive == "current"
        display_path_intact = displayed_reply == LOOPBACK_REPLY
        evidence = {
            "measurement": "transport-only",
            "event": self.event,
            "carrier_path": str(carrier_file),
            "carrier_chars": len(carrier),
            "carrier_lines": len(carrier.splitlines()),
            "summary_carried_unchanged": facts.get("summary_byte_identical", False),
            "summary_chars": len(expected_summary),
            "carrier_facts": facts,
            "baseline_source": baseline_source,
            "directive_observed": directive,
            "current_short_directive_present": CURRENT_DIRECTIVE in carrier,
            "old_show_as_written_directive_present": OLD_DIRECTIVE in carrier,
            "screen_reply": displayed_reply,
            "screen_reply_chars": len(displayed_reply),
            "screen_reply_lines": len(displayed_reply.splitlines()),
            "screen_reply_captured": bool(displayed_reply),
            "screen_non_reply_path": str(screen_file),
            "hearting_screen_text": screen_remainder,
            "hearting_screen_chars": len(screen_remainder),
            "hearting_screen_lines": len(screen_remainder.splitlines()),
            "display_path_intact": display_path_intact,
        }
        if directive == "old":
            evidence["derived"] = derived_old_minimum(self.event)
        run.evidence["completion_close"] = evidence
        carrier_ok = (facts.get("summary_byte_identical", False)
                      and facts.get("expected_sentinel_present", False)
                      and facts.get("record_sentinel_present", False)
                      and facts.get("unexpected_event_sentinels_absent", False))
        if verdict == "pass" and not (carrier_ok and directive_valid and display_path_intact):
            verdict = "fail"
            run.note("delivered carrier facts/directive or loopback display path did not match")
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
