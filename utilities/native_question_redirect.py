#!/usr/bin/env python3
"""A native question tool call becomes this session's Hearting question.

Claude `AskUserQuestion`, Codex `request_user_input` and OpenCode `question` are
intercepted before they run (Claude/Codex `PreToolUse`, OpenCode `tool.execute.before`).
The question is registered on the same board as `hearting run questions ask`, so the
panel, Fleet and the input line can answer it, and the native call is refused with one short
line: the question id and "end the turn". Nothing else is asked of the agent.

A native call that asks again what this session's open Hearting question asks opens no window
beside it (11-27: the agent registered a question, asked the same one natively in the same turn,
and the answer given on the board never reached a session standing in the native window). It is
refused with that question's id, and nothing is registered or replaced, while the person can
answer that question or the answer they gave in this turn is on its way. A different question
goes through as before (several open questions per session are a separate design).

Registration never blocks a question: when it cannot be written (no seat, no Hearting
state, a timeout, an invalid shape) and no such question is open, the native call goes
through untouched, so the person always sees the question. Registered workers and native
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

BUDGET_SECONDS = 5          # hook timeouts are 10 s; the question must never wait on the board for long
_REFIRE_SECONDS = 0.05      # once the budget is spent the alarm rings again until the decision has left
MAX_CHOICES = 8
_RECOMMENDED = re.compile(r"\s*[(（]\s*(?:권장|recommended)\s*[)）]\s*$", re.IGNORECASE)
_PREVIEW_LIMIT = 240
_TOOLS = {"claude": {"AskUserQuestion"}, "codex": {"request_user_input", "request_user_input_async"},
          "opencode": {"question"}}


class Skip(Exception):
    """Nothing to register: the native call goes through."""


class _Late(BaseException):
    """The budget is spent. Not an `Exception`, so no `except Exception`/`except OSError` on the way
    (seat resolution, state reads) takes it for an ordinary failure and goes on."""


class Waiting(Exception):
    """This session already has this question open: the native call is refused with its id."""

    def __init__(self, signal: dict):
        super().__init__(signal["id"])
        self.signal = signal


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


def _body(text) -> str:
    """A question's text as compared: its line endings made one and the whole stripped (case is kept)."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _open_questions(harness: str, session_id: str) -> list:
    """This session's open Hearting (message) questions, newest first, read without a seat or a lock."""
    import human_wait_state as HWS
    rows = [signal for signal in HWS.read_signals() if signal.get("mode") == "message"
            and signal.get("harness") == harness and signal.get("session_id") == session_id]
    return rows[::-1]


def _holds(signal: dict, sequence: int | None) -> bool:
    """Whether the board can take this question's answer now, as the board itself judges it
    (`read_selected_request`: open and answerable there), or the answer given in the turn that asked
    it is being delivered (`submitted`). Anything not confirmed (no board record yet, a session
    lineage or an answer record that cannot be read, a delivery unconfirmed, no turn known) is no."""
    import human_wait_state as HWS
    entry, known = HWS._selected_lookup(HWS._message_envelope(signal))
    if not known or entry is None:
        return False  # not on the board yet: the same registration writes it again
    board = HWS.read_selected_request({"id": signal["id"], "harness": signal["harness"],
                                       "session_id": signal["session_id"]})
    if board.get("actions"):
        return True  # the board takes an answer to it now
    return (board.get("state") == "submitted" and sequence is not None
            and int(signal.get("sequence") or 0) >= sequence)


def same_waiting(harness: str, session_id: str, rows: list, *, sequence: int | None = None) -> dict | None:
    """The open question of this session that asks what this native call asks and that the board can
    take an answer for now (`_holds`, which reads the question's current record by its id, so one
    replaced or answered just after it was read is never the one a refusal names), or None."""
    for found in _open_questions(harness, session_id):
        if same_question(found, rows) and _holds(found, sequence):
            return found
    return None


def same_question(signal: dict, rows: list) -> bool:
    """Whether the open question asks what this native call asks (DESIGN-QUESTIONS-MULTI-1127 §3).

    All of them must match: the text, the choice values in their order, the answer form and secrecy.
    The text is compared with the form this very call is registered in (`build`); a one-choice question
    also without the options' descriptions, so `questions ask` and the same question asked natively with
    descriptions are one question (a recommendation and a description are no answer value). The form is
    the one the board holds: one choice, or written. Several questions, a multi-select one or a preview
    are held as one written listing and are the same only as that whole listing (previews included); a
    written question that lists the same lines is another question. Nothing else is stripped or
    compared by likeness."""
    try:
        asked = ((signal.get("detail") or {}).get("questions") or [{}])[0]
        forms = [build(rows, detail=True)]
        if forms[0]["choices"]:
            forms.append(build(rows, detail=False))
        return (_body(signal.get("question")) in {_body(form["text"]) for form in forms}
                and list(asked.get("choices") or []) == forms[0]["choices"]
                and bool(asked.get("is_secret")) == any(row["secret"] for row in rows))
    except Exception:  # noqa: BLE001 -- unsure is another question: the native call goes through as before
        return False


def register(harness: str, session_id: str, rows: list, *, cwd: str | None = None,
             deadline: float | None = None) -> str:
    """Register the question on this session's seat; the id, or an error that means pass-through.

    `Waiting` when this session already has this question open (checked first, so a seat or
    state that cannot be written never puts a native window beside it)."""
    import human_wait_state as HWS
    import session_tidy as tidy
    cwd = cwd or os.getcwd()
    try:
        seat = tidy.resolve_seat(harness, cwd, os.environ, session_id, hook=True)
        sequence = tidy.read_prompt_seq(seat) if seat.key else None
    except Exception:  # noqa: BLE001 -- no seat: the same open question still refuses the native window
        seat, sequence = None, None
    held = same_waiting(harness, session_id, rows, sequence=sequence)
    if held is not None:
        raise Waiting(held)
    if seat is None or not seat.key:
        raise Skip("question-seat-unproven")
    sys.path.insert(0, str(ROOT / "tools"))
    from fleet.process_identity import current_launch_context
    launch = current_launch_context(harness, session_id)
    incarnation = {key: launch[key] for key in ("pid", "proc_start")} if launch else None
    for detail in (True, False):
        if deadline is not None and time.monotonic() >= deadline:
            raise _Late("native-question-redirect-timeout")   # spent where a handler swallowed it: no registration
        built = build(rows, detail=detail)
        try:
            value = HWS.register_message(
                harness, session_id, seat, built["text"], sequence=sequence,
                choices=built["choices"],
                recommended=built["recommend"] - 1 if built["recommend"] else None,
                cwd=cwd, incarnation=incarnation, replace=False)
            return value["id"]
        except ValueError as exc:
            if str(exc) == "question-open-elsewhere":
                held = same_waiting(harness, session_id, rows, sequence=sequence)  # registered meanwhile
                if held is not None:
                    raise Waiting(held) from None
            if str(exc) != "question-detail-too-large" or not detail:
                raise
    raise Skip("question-detail-too-large")


def reason_for(question_id: str) -> str:
    return (f"Registered as Hearting question {question_id}; it replaces this native question. "
            "End your turn now. The runtime brings the person's answer as the next prompt.")


def reason_already(question_id: str) -> str:
    return (f"This question is already Hearting question {question_id}. "
            "End your turn now. The runtime brings the person's answer as the next prompt.")


def redirect(harness: str, payload: dict, *, seconds: int = BUDGET_SECONDS) -> str | None:
    """The refusal text when the question was registered or is already open, else None (the native call goes through)."""
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

    armed = [False]

    def _late(_signal, _frame):
        if armed[0]:
            raise _Late("native-question-redirect-timeout")

    timed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    deadline = time.monotonic() + seconds if timed else None
    previous = None
    try:
        try:
            if timed:
                previous = signal.signal(signal.SIGALRM, _late)
                armed[0] = True
                # Seat resolution, the state reads and the registration all run inside one budget; a
                # handler on the way that swallows the expiry is rung out of again at once.
                signal.setitimer(signal.ITIMER_REAL, seconds, _REFIRE_SECONDS)
            decided = ("registered", register(harness, session_id, rows, cwd=cwd, deadline=deadline))
        except Waiting as held:
            decided = ("already", held.signal["id"])
        finally:
            armed[0] = False
            if timed:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
    except BaseException:  # noqa: BLE001 -- registration failed or ran late: show the native question
        return None
    if decided[0] == "already":
        # A refusal is decided within the budget or not at all, even where a handler on the way swallowed
        # the expiry: the native question shows, as on main. (A registration finished is on the board.)
        return reason_already(decided[1]) if deadline is None or time.monotonic() < deadline else None
    return reason_for(decided[1])


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
