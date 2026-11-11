#!/usr/bin/env python3
"""Isolated transport probes for concise completion-close instructions."""
from __future__ import annotations

from pathlib import Path
import re

from harness_behavior_cases_a import CompletionReceiptScenario


class CompletionCloseScenario(CompletionReceiptScenario):
    event = "success"
    event_context = "The work completed successfully; nothing remains."
    summary = "Capture the completion-close carrier and private TUI output for one outcome."

    def __init__(self):
        super().__init__()
        self.PROMPT = ("SCENARIO-RECEIPT start the registered child and end your turn. "
                       f"Recorded outcome: {self.event_context}")
        self.PROBE = f"SCENARIO-CLOSE-{self.event.upper()} anything new?"

    def drive(self, run):
        verdict = super().drive(run)
        requests = run.main_requests()
        carrier = next((r["last_text"] for r in reversed(requests)
                        if self.attempt in r.get("last_text", "")
                        and "First close:" in r.get("last_text", "")), "")
        out = Path(run.output) / run.harness / f"completion-close-{self.event}.carrier.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(carrier, encoding="utf-8")
        if verdict == "pass" and (not carrier or "one or two sentences" not in carrier):
            verdict = "fail"
            run.note("completion-close carrier was absent or did not contain the concise contract")
        run.evidence["completion_close"] = {
            "event": self.event,
            "carrier_path": str(out),
            "carrier_chars": len(carrier),
            "carrier_lines": len(carrier.splitlines()),
            "first_close_instruction_present": "one or two sentences" in carrier,
            "records_not_reproduced_instruction_present": "do not reproduce it for the person" in carrier,
            "preserved_facts": {
                "failure": self.event != "failure" or bool(re.search(r"fail|error|failed", carrier, re.I)),
                "unfinished": self.event != "partial" or bool(re.search(r"partial|could not|unfinished|remaining", carrier, re.I)),
                "decision": self.event != "decision" or bool(re.search(r"decision|must make", carrier, re.I)),
            },
        }
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
