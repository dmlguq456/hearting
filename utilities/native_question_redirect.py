#!/usr/bin/env python3
"""A native question tool call becomes this session's Hearting question.

Claude `AskUserQuestion`, Codex `request_user_input` and OpenCode `question` are
intercepted before they run (Claude/Codex `PreToolUse`, OpenCode `tool.execute.before`).
The question is registered on the same board as `hearting run questions ask`, so the
panel, Fleet and the input line can answer it, and the native call is refused with one short
line: the question id and "end the turn". Nothing else is asked of the agent.

Registration never blocks a question: when it cannot be written (no seat, no Hearting
state, a timeout, an invalid shape) the native call goes through untouched, so the person
always sees the question. A session that already has a different waiting Hearting question keeps it (the native call
goes through; a new registration would silently cancel it). Registered workers and native
subagents do nothing here, and a secret question is left to its own surface.

One native call may carry several questions; a Hearting question record holds one. A single
plain question keeps its choices and its recommendation (a label ending `(권장)` or
`(Recommended)`). Several questions, a multi-select question or a preview are written as one
text question the person answers in words, with the options and their descriptions listed.

Usage (stdin: the harness payload):
  native_question_redirect.py --claude|--codex   print the PreToolUse deny JSON when registered
  native_question_redirect.py --opencode         print the reason and exit 1 when registered
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

BUDGET_SECONDS = 5          # hook timeouts are 10 s; the question must never wait on the board for long
MAX_CHOICES = 8
_RECOMMENDED = re.compile(r"\s*[(（]\s*(?:권장|recommended)\s*[)）]\s*$", re.IGNORECASE)
_PREVIEW_LIMIT = 240
_TOOLS = {"claude": {"AskUserQuestion"}, "codex": {"request_user_input", "request_user_input_async"},
          "opencode": {"question"}}


class Skip(Exception):
    """Nothing to register: the native call goes through."""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized(harness: str, payload: dict) -> tuple[str, list]:
    """`(session id, [{question, header, options:[{label, description, preview}], multi, secret}])`."""
    if harness == "opencode":
        name, session = payload.get("tool"), payload.get("sessionID")
        args = payload.get("args")
    else:
        name, session = payload.get("tool_name"), payload.get("session_id")
        args = payload.get("tool_input")
    name = str(name or "").rsplit(".", 1)[-1]
    if name not in _TOOLS[harness] or not isinstance(session, str) or not session:
        raise Skip("not-a-question-call")
    listed = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(listed, list) or not listed:
        raise Skip("no-questions")
    rows = []
    for raw in listed:
        if not isinstance(raw, dict) or not _text(raw.get("question")):
            raise Skip("question-text-missing")
        options = []
        for option in raw.get("options") or ():
            if isinstance(option, str):
                option = {"label": option}
            if not isinstance(option, dict) or not _text(option.get("label")):
                raise Skip("option-label-missing")
            options.append({"label": _text(option["label"]), "description": _text(option.get("description")),
                            "preview": _text(option.get("preview"))})
        rows.append({"question": _text(raw["question"]), "header": _text(raw.get("header")),
                     "options": options,
                     "multi": bool(raw.get("multiSelect") or raw.get("multiple")),
                     "secret": bool(raw.get("isSecret") or raw.get("is_secret") or raw.get("secret"))})
    return session, rows


def _is_registered_worker(payload: dict) -> bool:
    """Registered workers and native subagents keep their own behavior (nothing happens)."""
    import session_tidy as tidy
    return (tidy.is_worker() or os.environ.get("AGENT_SESSION_ROLE", "").lower()
            in {"owner", "worker", "auxiliary", "reviewer"} or bool(payload.get("agent_id")))


def _line(option: dict, number: int | None, *, detail: bool) -> str:
    label = option["label"]
    head = (f"{number}. " if number else "- ") + label
    if detail and option["description"]:
        head += " — " + option["description"]
    if detail and option["preview"]:
        preview = " ".join(option["preview"].split())
        head += "\n    " + (preview[:_PREVIEW_LIMIT] + ("…" if len(preview) > _PREVIEW_LIMIT else ""))
    return head


def build(rows: list, *, detail: bool = True) -> dict:
    """`{text, choices, recommend}` for one Hearting question (`recommend` is 1-based or None)."""
    only = rows[0] if len(rows) == 1 else None
    if (only is not None and not only["multi"] and 1 <= len(only["options"]) <= MAX_CHOICES
            and not any(option["preview"] for option in only["options"])):
        labels = [_RECOMMENDED.sub("", option["label"]).strip() or option["label"]
                  for option in only["options"]]
        if len(set(labels)) == len(labels):
            recommend = next((index + 1 for index, option in enumerate(only["options"])
                              if _RECOMMENDED.search(option["label"])), None)
            lines = [only["question"]]
            described = [f"- {label}: {option['description']}" for label, option in
                         zip(labels, only["options"]) if detail and option["description"]]
            return {"text": "\n".join(lines + described), "choices": labels, "recommend": recommend}
    blocks = []
    for index, row in enumerate(rows, 1):
        title = (f"Q{index}. " if len(rows) > 1 else "") + row["question"]
        if row["multi"]:
            title += "  [several may be chosen]"
        lines = [title] + [_line(option, number, detail=detail)
                           for number, option in enumerate(row["options"], 1)]
        blocks.append("\n".join(lines))
    return {"text": "\n\n".join(blocks), "choices": [], "recommend": None}


def register(harness: str, session_id: str, rows: list, *, cwd: str | None = None) -> str:
    """Register the question on this session's seat; the id, or an error that means pass-through."""
    import human_wait_state as HWS
    import session_tidy as tidy
    cwd = cwd or os.getcwd()
    seat = tidy.resolve_seat(harness, cwd, os.environ, session_id, hook=True)
    if not seat.key:
        raise Skip("question-seat-unproven")
    sys.path.insert(0, str(ROOT / "tools"))
    from fleet.process_identity import current_launch_context
    launch = current_launch_context(harness, session_id)
    incarnation = {key: launch[key] for key in ("pid", "proc_start")} if launch else None
    for detail in (True, False):
        built = build(rows, detail=detail)
        try:
            value = HWS.register_message(
                harness, session_id, seat, built["text"], sequence=tidy.read_prompt_seq(seat),
                choices=built["choices"],
                recommended=built["recommend"] - 1 if built["recommend"] else None,
                cwd=cwd, incarnation=incarnation, replace=False)
            return value["id"]
        except ValueError as exc:
            if str(exc) != "question-detail-too-large" or not detail:
                raise
    raise Skip("question-detail-too-large")


def reason_for(question_id: str) -> str:
    return (f"Registered as Hearting question {question_id}; it replaces this native question. "
            "End your turn now. The runtime brings the person's answer as the next prompt.")


def redirect(harness: str, payload: dict, *, seconds: int = BUDGET_SECONDS) -> str | None:
    """The refusal text when the question was registered, else None (the native call goes through)."""
    if not isinstance(payload, dict):
        return None
    try:
        if _is_registered_worker(payload):
            return None
        session_id, rows = _normalized(harness, payload)
        if any(row["secret"] for row in rows):
            return None
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else None
    except Exception:  # noqa: BLE001 -- any doubt leaves the native question alone
        return None

    def _late(_signal, _frame):
        raise TimeoutError("native-question-redirect-timeout")

    timed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    previous = None
    try:
        if timed:
            previous = signal.signal(signal.SIGALRM, _late)
            signal.setitimer(signal.ITIMER_REAL, seconds)
        return reason_for(register(harness, session_id, rows, cwd=cwd))
    except BaseException:  # noqa: BLE001 -- registration failed or ran late: show the native question
        return None
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


def main(argv=None, stdin=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    harness = next((flag[2:] for flag in argv if flag in ("--claude", "--codex", "--opencode")), "")
    if harness not in _TOOLS:
        return 0
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
    except (OSError, ValueError):
        return 0
    reason = redirect(harness, payload)
    if reason is None:
        return 0
    if harness == "opencode":
        print(reason)
        return 1
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}},
                     ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
