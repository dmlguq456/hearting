#!/usr/bin/env python3
"""A native question tool call becomes this session's Hearting question.

Claude `AskUserQuestion`, Codex `request_user_input` and OpenCode `question` are
intercepted before they run (Claude/Codex `PreToolUse`, OpenCode `tool.execute.before`).
The question is registered on the same board as `hearting run questions ask`, so the
panel, Fleet and the input line can answer it. A call has one of three outcomes (`decide`), and
each hook carries it as it is:

- registered: a new id, or the id of the same question already open in this session (text up to
  line ends and edge spaces, choice values in order, answer form, secrecy; a record an earlier
  release wrote with the option descriptions in its text is the same only as that exact text,
  rebuilt from this call). The
  native call is refused with one short line: the id, "end the turn". Another wait of the session
  (a Hearting question, a route gate, a native wait), a retry or a late screen is never a reason
  to let the native question open.
- pass: registration failed before anything was written (a shape a record cannot carry, no proven
  seat, a refused or failed write, this very registration already ended at this prompt, the budget
  ran out) and the session's questions were read
  without a same one. The call's private identity reference goes onto the session's native wait
  marker (`fleet.interaction`, no text) and the native question opens, counted as that wait; the
  same call again is told it is open there. The marker's own clear ends the reference.
- unknown: the question storage cannot be read, a write's result was lost and cannot be read
  back, or the marker cannot be written. Nothing opens; the refusal says so once, and the same
  call again reads as one of the other two.

Registered workers and native subagents do nothing here.

One native call may carry several questions; a Hearting question record holds one. A single
question keeps its choices, its recommendation (a label ending `(권장)` or `(Recommended)`) and
its secrecy, and each option's description goes with its choice (the board draws it under the
choice's name; 11-27 design 가), not into the question's text. Several questions, a multi-select
question or a preview are written as one text question the person answers in words, with the
options and their descriptions listed; several questions with a secret among them cannot keep
it apart, so they pass.

Usage (stdin: the harness payload):
  native_question_redirect.py --claude|--codex   print the PreToolUse deny JSON when refused
  native_question_redirect.py --opencode         print the reason and exit 1 when refused; stderr
                                                 says when registering began (a check that dies
                                                 after it is unknown, before it a pass)
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

BUDGET_SECONDS = 5          # hook timeouts are 10 s (OpenCode 8 s); registration never waits longer
SETTLE_SECONDS = 1          # then reading back what a failed registration left, and marking the native wait
REGISTERING = "native-question-redirect: registering"
MAX_CHOICES = 8
_RECOMMENDED = re.compile(r"\s*[(（]\s*(?:권장|recommended)\s*[)）]\s*$", re.IGNORECASE)
_PREVIEW_LIMIT = 240
_TOOLS = {"claude": {"AskUserQuestion"}, "codex": {"request_user_input", "request_user_input_async"},
          "opencode": {"question"}}
_WAIT_SOURCE = {"claude": "claude-asktool", "codex": "codex-question", "opencode": "opencode-question"}


class Skip(Exception):
    """Not a question call of a person's session: nothing happens here."""


class Unfit(Exception):
    """Registration failed before anything was written."""


class Outcome(NamedTuple):
    kind: str                   # "none" | "registered" | "native" | "pass" | "unknown"
    reason: str | None = None   # the refusal line (registered, native, unknown)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized(harness: str, payload: dict) -> tuple[str, list | None]:
    """`(session id, [{question, header, options:[{label, description, preview}], multi, secret}])`;
    the list is None when a question record cannot carry the call's shape."""
    if harness == "opencode":
        name, session = payload.get("tool"), payload.get("sessionID")
        args = payload.get("args")
    else:
        name, session = payload.get("tool_name"), payload.get("session_id")
        args = payload.get("tool_input")
    name = str(name or "").rsplit(".", 1)[-1]
    if (name not in _TOOLS[harness] or not isinstance(session, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", session)):
        raise Skip("not-a-question-call")        # no session id a question or a wait can be kept under
    listed = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(listed, list) or not listed:
        return session, None
    rows = []
    for raw in listed:
        if not isinstance(raw, dict) or not _text(raw.get("question")):
            return session, None
        options = []
        for option in raw.get("options") or ():
            if isinstance(option, str):
                option = {"label": option}
            if not isinstance(option, dict) or not _text(option.get("label")):
                return session, None
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
    """`{text, choices, recommend, descriptions, secret}` for one Hearting question (`recommend` is
    1-based or None; `descriptions` maps a choice to its option's description, None when there is
    none or `detail` is off)."""
    only = rows[0] if len(rows) == 1 else None
    secret = bool(only and only["secret"])
    if not only and any(row["secret"] for row in rows):
        raise Unfit("question-secret-among-several")
    if (only is not None and not only["multi"] and 1 <= len(only["options"]) <= MAX_CHOICES
            and not any(option["preview"] for option in only["options"])):
        labels = [_RECOMMENDED.sub("", option["label"]).strip() or option["label"]
                  for option in only["options"]]
        if len(set(labels)) == len(labels):
            recommend = next((index + 1 for index, option in enumerate(only["options"])
                              if _RECOMMENDED.search(option["label"])), None)
            described = {label: option["description"] for label, option in zip(labels, only["options"])
                         if detail and option["description"]}
            return {"text": only["question"], "choices": labels, "recommend": recommend,
                    "descriptions": described or None, "secret": secret}
    blocks = []
    for index, row in enumerate(rows, 1):
        title = (f"Q{index}. " if len(rows) > 1 else "") + row["question"]
        if row["multi"]:
            title += "  [several may be chosen]"
        lines = [title] + [_line(option, number, detail=detail)
                           for number, option in enumerate(row["options"], 1)]
        blocks.append("\n".join(lines))
    return {"text": "\n\n".join(blocks), "choices": [], "recommend": None, "descriptions": None, "secret": secret}


def _key(built: dict) -> tuple:
    """`human_wait_state._question_key` of the record this build would register."""
    import human_wait_state as HWS
    return HWS._question_key({"detail": {"questions": [{
        "question": built["text"], "choices": built["choices"], "is_other": True, "is_secret": built["secret"]}]}})


def _keys(rows: list) -> set:
    """This call's same-question candidates: its record with and without the option descriptions, and
    the text an earlier release wrote (each description a `- choice: words` line after the question)."""
    builds = [build(rows, detail=detail) for detail in (True, False)]
    words = builds[0]["descriptions"] or {}
    if words:
        builds.append(dict(builds[0], text="\n".join([builds[0]["text"]] + [
            f"- {choice}: {words[choice]}" for choice in builds[0]["choices"] if choice in words])))
    return {_key(built) for built in builds}


def _same_open(found: dict, keys: set):
    """The oldest open question of this session that is the same as one of `keys`, or None."""
    return next((row for row in sorted(found.values(), key=lambda row: (row["since"], row["id"]))
                 if row.get("lifecycle") == "open" and _key_of(row) in keys), None)


def _key_of(row: dict):
    import human_wait_state as HWS
    try:
        return HWS._question_key(row)
    except (KeyError, IndexError, TypeError):
        return None


def register(harness: str, session_id: str, rows: list, *, cwd: str | None = None,
             seen: dict | None = None) -> str:
    """The id of this question on this session's seat: the same one already open, else a new one.
    Every error is raised before the write (`Unfit`) or reads back (`decide`). `seen` gets the seat."""
    import human_wait_state as HWS
    import session_tidy as tidy
    cwd = cwd or os.getcwd()
    builds = [build(rows, detail=detail) for detail in (True, False)]
    seat = tidy.resolve_seat(harness, cwd, os.environ, session_id, hook=True)
    if not seat.key:
        raise Unfit("question-seat-unproven")
    if seen is not None:
        seen["seat_key"] = seat.key
    with tidy.seat_lock(seat.key):
        found, state = HWS.message_questions(harness, session_id, seat.key)
        if state != "ok":
            raise OSError("question-storage-unreadable")
        same = _same_open(found, _keys(rows))
    if same:
        return same["id"]
    sys.path.insert(0, str(ROOT / "tools"))
    from fleet.process_identity import current_launch_context
    launch = current_launch_context(harness, session_id)
    incarnation = {key: launch[key] for key in ("pid", "proc_start")} if launch else None
    for built in builds:
        try:
            value = HWS.register_message(
                harness, session_id, seat, built["text"], sequence=tidy.read_prompt_seq(seat),
                choices=built["choices"],
                recommended=built["recommend"] - 1 if built["recommend"] else None,
                descriptions=built["descriptions"], secret=built["secret"], cwd=cwd, incarnation=incarnation)
            if value.get("lifecycle") != "open":     # this very registration already ended at this prompt
                raise Unfit("question-already-ended")
            return value["id"]
        except ValueError as exc:
            if str(exc) != "question-detail-too-large" or built is builds[-1]:
                raise Unfit(str(exc)) from exc
    raise Unfit("question-detail-too-large")


def reason_for(question_id: str) -> str:
    return (f"Registered as Hearting question {question_id}; it replaces this native question. "
            "End your turn now. The answer comes as a later prompt marked with this id.")


def reason_native() -> str:
    return ("This question is already open as a native question (Hearting could not register it); "
            "the person answers it there. End your turn now.")


def reason_unknown(why: str) -> str:
    return (f"Hearting could not confirm whether this question was registered ({why}). Make the same "
            "call again: it then names the Hearting question or lets the native question open "
            "(`hearting run questions ask` registers it too).")


@contextlib.contextmanager
def _budget(seconds: float):
    """Raise `TimeoutError` inside the block after `seconds` (main thread only)."""
    def _late(_signal, _frame):
        raise TimeoutError("native-question-redirect-timeout")

    timed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGALRM, _late) if timed else None
    if timed:
        signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


def _call_ref(harness: str, session_id: str, payload: dict) -> str:
    """The private identity reference of this exact call (a digest; no text leaves this process)."""
    args = payload.get("args") if harness == "opencode" else payload.get("tool_input")
    raw = json.dumps([harness, session_id, args], ensure_ascii=False, sort_keys=True, default=str)
    return "nq-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _interaction():
    sys.path.insert(0, str(ROOT / "tools"))
    from fleet import interaction
    return interaction


def decide(harness: str, payload, *, seconds: float = BUDGET_SECONDS, settle: float = SETTLE_SECONDS,
           announce=None) -> Outcome:
    """The call's outcome (see the module text); never raises. `announce()` runs as registering begins."""
    if not isinstance(payload, dict) or harness not in _TOOLS:
        return Outcome("none")
    try:
        if _is_registered_worker(payload):
            return Outcome("none")
        session_id, rows = _normalized(harness, payload)
        ref = _call_ref(harness, session_id, payload)
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else None
    except Exception:  # noqa: BLE001 -- not a question call of a person's session
        return Outcome("none")
    try:
        if (_interaction().read_wait(session_id, harness) or {}).get("call_ref") == ref:
            return Outcome("native", reason_native())
    except Exception:  # noqa: BLE001 -- an unreadable marker holds no reference
        pass
    keys, seen = set(), {}
    try:
        with _budget(seconds):
            if rows is None:
                raise Unfit("question-shape")
            keys = _keys(rows)
            if announce:
                announce()
            return Outcome("registered", reason_for(register(harness, session_id, rows, cwd=cwd, seen=seen)))
    except BaseException:  # noqa: BLE001 -- what it left is read back below
        pass
    marked = False
    try:
        with _budget(settle):
            if keys:
                import human_wait_state as HWS
                found, state = HWS.message_questions(harness, session_id, seen.get("seat_key"))
                if state != "ok":
                    return Outcome("unknown", reason_unknown("its question storage cannot be read"))
                same = _same_open(found, keys)
                if same:
                    return Outcome("registered", reason_for(same["id"]))
            marked = _interaction().set_wait(session_id, harness, "decision", _WAIT_SOURCE[harness], call_ref=ref)
            if not marked:
                return Outcome("unknown", reason_unknown("its native wait cannot be marked"))
            return Outcome("pass")
    except BaseException:  # noqa: BLE001 -- the read-back itself failed or ran late
        return Outcome("pass") if marked else Outcome("unknown", reason_unknown("its check did not finish"))


def redirect(harness: str, payload: dict, *, seconds: float = BUDGET_SECONDS) -> str | None:
    """The refusal line, or None when the native call goes through."""
    return decide(harness, payload, seconds=seconds).reason


def main(argv=None, stdin=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    harness = next((flag[2:] for flag in argv if flag in ("--claude", "--codex", "--opencode")), "")
    if harness not in _TOOLS:
        return 0
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
    except (OSError, ValueError):
        return 0
    announce = (lambda: print(REGISTERING, file=sys.stderr, flush=True)) if harness == "opencode" else None
    reason = decide(harness, payload, announce=announce).reason
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
