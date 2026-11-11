#!/usr/bin/env python3
"""Token self-regulation behaviour scenario for ``harness_behavior_scenarios.py``.

``token-budget``
    Five prompts on a 200,000-token model.  The loopback model reports the
    prompt size of each answer: 1,000, 150,000, 150,000, 175,000, 175,000.
    The harness judges at each prompt from the previous request (spec
    ``token-self-regulation`` Phase 1: 70/85% bands, transition only), so the
    expected model requests are: none, none, ``TOKEN_BUDGET=tight``, none
    (same band), ``TOKEN_BUDGET=critical``.  Pass: exactly that sequence.
    Gap: no directive ever reaches the model.  Fail: any other sequence.
    The bounded content-free accounting aggregate
    (``$XDG_STATE_HOME/hearting/token-budget/accounting``) is attached as
    evidence: five invocations, two emissions.

    Percentages follow each harness's own footer: Codex keeps its 12,000-token
    reserve ((150,001 - 12,000) / 188,000 = 73%, 175,001 → 87%); OpenCode
    divides by the model's ``limit.context`` (75%, 88%).  Both cross the same
    bands at the same prompts.

``context-alert``
    The same five prompts on a 1,000,000-token model reporting 1,000, 520,000,
    520,000, 720,000, 720,000.  The shared 500K/700K work-preservation notice
    (``utilities/context_alert.py``, absolute input tokens) is due at the third
    prompt (``약 520K``) and the fifth (``약 720K``), once each.  Pass: exactly
    that sequence in the model requests.

Synthetic data: the fixture HOME's model settings only; no registry rows.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario  # noqa: E402

MARK = "SCENARIO-TOKEN-BUDGET"
USAGE = (1_000, 150_000, 150_000, 175_000, 175_000)
EXPECTED = (None, None, "tight", None, "critical")
WINDOW = 200_000
DIRECTIVE = re.compile(r"TOKEN_BUDGET=(tight|critical)")
ALERT = re.compile(r"입력 문맥 약 (\d+)K")


def directives(text: str, pattern=DIRECTIVE) -> list[str]:
    return pattern.findall(text or "")


def prompt_index(record) -> int | None:
    """The newest scenario prompt in the request. Codex and Claude add hook
    context after the person's text, so the last input item is not always it."""
    found = re.findall(MARK + r" (\d)", record.get("text") or "")
    return int(found[-1]) if found else None


def new_directives(request, previous, pattern=DIRECTIVE) -> list[str]:
    """Directives this request adds. Codex and Claude keep earlier hook context in
    the conversation; OpenCode's system block holds only the current turn's."""
    added = Counter(directives(request["text"], pattern))
    if previous is not None:
        added -= Counter(directives(previous["text"], pattern))
    return sorted(added.elements())


class TokenBudgetScenario(Scenario):
    name = "token-budget"
    summary = ("Context crossing 70% then 85% sends the tight, then the critical directive "
               "once each, at the same prompts on every harness.")
    usage, expected, window, pattern = USAGE, EXPECTED, WINDOW, DIRECTIVE
    missing = "no token-budget directive reached the model"

    def prepare(self, run):
        env = run.home.env
        if run.harness == "codex":
            # The fixture model's window, and no native auto-compaction inside it.
            path = Path(env["CODEX_HOME"]) / "config.toml"
            text = path.read_text(encoding="utf-8")
            path.write_text(f"model_context_window = {self.window}\n"
                            "model_auto_compact_token_limit = 10000000\n" + text,
                            encoding="utf-8")
        elif run.harness == "claude":
            env["DISABLE_AUTO_COMPACT"] = "1"
        elif run.harness == "opencode":
            path = Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            for model in config["provider"]["fixture"]["models"].values():
                model["limit"]["context"] = self.window
            path.write_text(json.dumps(config, indent=1), encoding="utf-8")

    def script(self, run):
        def answer(record, earlier):
            index = prompt_index(record) or 0
            return Reply(f"OK {index}",
                         input_tokens=self.usage[min(index, len(self.usage) - 1)])
        return answer

    def drive(self, run):
        seen = []
        previous = None
        for index in range(len(self.usage)):
            before = len(run.main_requests())
            run.prompt(f"{MARK} {index}")
            if not run.wait_requests(before + 1, 60):
                run.note(f"prompt {index}: no model request")
                return "error"
            run.wait_idle(quiet=4, timeout=60)
            rows = [r for r in run.main_requests() if prompt_index(r) == index]
            found = new_directives(rows[0], previous, self.pattern) if rows else []
            previous = rows[-1] if rows else previous
            seen.append(found[0] if len(found) == 1 else (found or None))
            time.sleep(1)
        run.evidence["directive_by_prompt"] = seen
        run.evidence["expected_by_prompt"] = list(self.expected)
        state = Path(run.home.env.get("XDG_STATE_HOME")
                     or Path(run.home.env["HOME"]) / ".local" / "state")
        accounting = state / "hearting" / "token-budget" / "accounting"
        aggregates = []
        for path in sorted(accounting.glob("*.json")) if accounting.is_dir() else []:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            aggregates.append({k: value.get(k) for k in (
                "adapter", "hook_invocations", "emissions", "zero_injections",
                "zero_reason_counts", "directive_utf8_bytes_total")})
        run.evidence["accounting"] = aggregates
        if list(self.expected) == seen:
            return "pass"
        if not any(seen):
            run.note(self.missing)
            return "gap"
        run.note(f"directives {seen} != expected {list(self.expected)}")
        return "fail"


class ContextAlertScenario(TokenBudgetScenario):
    name = "context-alert"
    summary = ("Input crossing 500K then 700K brings the shared work-preservation notice "
               "once each, at the same prompts on every harness.")
    usage = (1_000, 520_000, 520_000, 720_000, 720_000)
    expected = (None, None, "520", None, "720")
    window = 1_000_000
    pattern = ALERT
    missing = "no 500K/700K notice reached the model"


SCENARIOS = (TokenBudgetScenario, ContextAlertScenario)
