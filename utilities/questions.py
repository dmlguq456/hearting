#!/usr/bin/env python3
"""Open questions waiting on the person, and their answers sent the existing way.

``hearting run questions`` lists the same board Fleet's QUESTIONS view shows (one
shared projection, one count): each wait's question, choices and recommended answer,
who answers it now, and whether it is current, older or ownerless. A steward relays
that summary with the recommendation to its person through its own native question
surface; it never answers on the person's behalf.

``hearting run questions answer ID ...`` sends the person's chosen answer through the
same selected-request path Fleet uses (``human_wait_state.submit_selected_response``):
S2 releases the route gate, S3 continues the blocked owner with a correction, S1 Codex
and OpenCode questions use their native reply. A wait without a reply surface (a Claude
native question, an S4 marker) is reported with its reason and left for that session.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import human_wait_state as HWS  # noqa: E402

_CACHE_WAIT_SECONDS = 15.0
_DELIVERED = {"applied", "received", "submitted"}
_KIND_LABEL = {"S1": "native question", "S2": "human gate", "S3": "blocked owner",
               "S4": "marked question"}
_OWNER_NOTE = {"successor": "successor", "none": "owner ended, no successor",
               "unknown": "owner unobserved"}


def _age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd %dh" % (seconds // 86400, seconds % 86400 // 3600)


def load_board(*, wait=None):
    """The shared projection Fleet reads, or None while it is unobserved.

    A missing or expired projection asks the existing producer for one pass and
    waits a bounded moment for its publication; it never computes a second view."""
    wait = _CACHE_WAIT_SECONDS if wait is None else wait
    deadline = time.monotonic() + max(0.0, wait)
    view = HWS.cached_view(request=True)
    while view is None and time.monotonic() < deadline:
        time.sleep(0.5)
        view = HWS.cached_view(request=False)
    return view


def _choice_label(choice):
    if isinstance(choice, dict):
        return str(choice.get("label") or choice.get("value") or "")
    return str(choice)


def _choice_value(choice):
    if isinstance(choice, dict):
        value = choice.get("value", choice.get("label"))
        return value if isinstance(value, str) else str(value)
    return str(choice)


def questions_of(envelope):
    """``[{id, question, choices, recommended}]`` from one selected request envelope."""
    detail = envelope.get("detail") if isinstance(envelope, dict) else None
    listed = detail.get("questions") if isinstance(detail, dict) else None
    if isinstance(listed, list) and listed:
        out = []
        for question in listed:
            if not isinstance(question, dict) or question.get("id") is None:
                continue
            choices = question.get("choices") if isinstance(question.get("choices"), list) else []
            recommended = question.get("recommended")
            out.append({"id": str(question["id"]), "question": str(question.get("question") or ""),
                        "choices": choices,
                        "recommended": recommended if type(recommended) is int
                        and 0 <= recommended < len(choices) else None,
                        "free_text": bool(question.get("is_other", not choices))})
        return out
    question_id = envelope.get("question_id") if isinstance(envelope, dict) else None
    if question_id is None:
        return []
    text = detail.get("question") if isinstance(detail, dict) else detail
    choices = envelope.get("choices")
    if not isinstance(choices, list) and isinstance(detail, dict):
        choices = detail.get("choices")
    choices = choices if isinstance(choices, list) else []
    return [{"id": str(question_id), "question": str(text or ""), "choices": choices,
             "recommended": None, "free_text": not choices}]


def _reference(item):
    owner = item.get("owner") or {}
    return {"id": item["id"], "harness": owner.get("harness"),
            "session_id": owner.get("session_id")}


def describe(item, *, now, detail=True):
    """One board row for a person-facing relay: identity, timing, owner and question."""
    owner = item.get("owner") or {}
    row = {"id": item.get("id"), "source": item.get("source"),
           "kind": _KIND_LABEL.get(item.get("source"), str(item.get("kind") or "wait")),
           "since": item.get("since"), "age": _age(now - float(item.get("since") or now)),
           "state": item.get("fold") or ("older" if item.get("stale") else "current"),
           "owner": {"harness": owner.get("harness"), "session_id": owner.get("session_id")},
           "owner_state": item.get("owner_state") or "unknown",
           "registered_owner": item.get("registered_owner"),
           "route_id": item.get("route_id"), "cwd": item.get("cwd"),
           "project": os.path.basename(str(item.get("cwd") or "").rstrip("/")) or None}
    if not detail:
        return row
    envelope = HWS.read_selected_request(_reference(item)) if row["owner"]["harness"] else None
    if not isinstance(envelope, dict):
        row.update(questions=[], answerable=False, reason="no exact owner session binding")
        return row
    support = envelope.get("support") if isinstance(envelope.get("support"), dict) else {}
    row["questions"] = questions_of(envelope)
    row["answer_kind"] = "decision" if envelope.get("kind") == "approval" else "answers"
    row["answerable"] = envelope.get("state") == "open" and support.get("supported") is True
    reason = support.get("reason") or envelope.get("reason")
    if not row["answerable"] and reason:
        row["reason"] = str(reason)
    if envelope.get("state") != "open":
        row["request_state"] = envelope.get("state")
    return row


def _owner_text(row):
    owner = row["owner"]
    text = "%s %s" % (owner.get("harness") or "?", str(owner.get("session_id") or "?")[:8])
    note = _OWNER_NOTE.get(row["owner_state"])
    registered = row.get("registered_owner")
    if row["owner_state"] == "successor" and isinstance(registered, dict):
        note = "successor of %s" % str(registered.get("session_id") or "?")[:8]
    return text + (" (%s)" % note if note else "")


def render(board, rows, folded):
    counts = HWS.wait_counts(board.get("items") or ())
    observed = time.time() - float(board.get("observed_at") or time.time())
    head = "questions: %s current · %d older" % (
        counts["total"] if board.get("complete") else "?", counts["older"])
    if counts["ownerless"]:
        head += " (%d ownerless)" % counts["ownerless"]
    head += " · observed %s ago" % _age(observed) if observed >= 60 else " · observed now"
    if not board.get("complete"):
        head += " · observation incomplete"
    lines = [head]
    for row in rows:
        place = " · " + row["project"] if row.get("project") else ""
        lines.append("%s  %s · %s%s · %s" % (row["id"], row["kind"], row["age"], place,
                                             _owner_text(row)))
        if row["state"] != "current":
            lines.append("  folded: %s" % row["state"])
        for index, question in enumerate(row.get("questions") or (), 1):
            prefix = "  q%d " % index if len(row["questions"]) > 1 else "  "
            lines.append(prefix + (question["question"] or "(question text is in that session)"))
            for number, choice in enumerate(question["choices"], 1):
                mark = "  (recommended)" if question["recommended"] == number - 1 else ""
                lines.append("      %d) %s%s" % (number, _choice_label(choice), mark))
        if row.get("answerable"):
            many = len(row.get("questions") or ()) > 1
            how = ("--decision approve|reject|defer" if row.get("answer_kind") == "decision"
                   else "--choice N per question" if many else "--choice N | --text-file FILE")
            lines.append("  answer: hearting run questions answer %s %s" % (row["id"], how))
        elif row.get("reason"):
            lines.append("  answer in that session: %s" % row["reason"])
    for row in folded:
        lines.append("%s  %s · %s · %s · %s" % (row["id"], row["kind"], row["age"],
                                                row["state"], _owner_text(row)))
    return "\n".join(lines)


def _list(args):
    board = load_board()
    if board is None:
        print("questions: unknown · the shared answer-wait observation is not available yet",
              file=sys.stderr)
        return 3
    now = time.time()
    items = sorted((item for item in board.get("items") or () if isinstance(item, dict)),
                   key=lambda item: (bool(item.get("stale")), float(item.get("since") or 0)))
    current = [describe(item, now=now) for item in items if not item.get("stale")]
    folded = [describe(item, now=now, detail=args.all) for item in items if item.get("stale")]
    if args.json:
        print(json.dumps({"observed_at": board.get("observed_at"),
                          "complete": bool(board.get("complete")),
                          "counts": HWS.wait_counts(board.get("items") or ()),
                          "items": current + folded}, ensure_ascii=False, indent=1))
        return 0
    if args.all:
        print(render(board, current + folded, []))
    else:
        print(render(board, current, folded))
    return 0


def _find(board, wanted):
    items = [item for item in board.get("items") or () if isinstance(item, dict)]
    exact = [item for item in items if item.get("id") == wanted]
    if exact:
        return exact[0], None
    matches = [item for item in items if str(item.get("id") or "").startswith(wanted)]
    if len(matches) == 1:
        return matches[0], None
    return None, ("no open question has this id" if not matches
                  else "id prefix matches %d questions; give more of the id" % len(matches))


def _answer_values(value):
    values = value if isinstance(value, list) else [value]
    return [item if isinstance(item, str) else json.dumps(item) for item in values]


def build_response(envelope, *, choices=(), text=None, decision=None, answers=None):
    """The portable response for ``submit_selected_response``, or ``(None, reason)``."""
    if envelope.get("kind") == "approval":
        if decision not in {"approve", "reject", "defer"}:
            return None, "this is an approval: give --decision approve|reject|defer"
        return {"decision": decision}, None
    if decision is not None:
        return None, "--decision is only for approvals"
    questions = questions_of(envelope)
    if not questions:
        return None, "the request has no answerable question"
    if answers is not None:
        if not isinstance(answers, dict) or not answers:
            return None, "--answers-file must hold a JSON object of question id to answer"
        return {"answers": {str(key): _answer_values(value)
                            for key, value in answers.items()}}, None
    if text is not None:
        if len(questions) != 1:
            return None, "--text answers one question; this request has %d" % len(questions)
        return {"answers": {questions[0]["id"]: [text]}}, None
    if not choices:
        return None, "give --choice, --text, --text-file or --answers-file"
    if len(choices) != len(questions):
        return None, "give one --choice per question (%d)" % len(questions)
    out = {}
    for question, number in zip(questions, choices):
        if not 1 <= number <= len(question["choices"]):
            return None, "choice %d is outside 1..%d for %s" % (
                number, len(question["choices"]), question["id"])
        out[question["id"]] = [_choice_value(question["choices"][number - 1])]
    return {"answers": out}, None


def _answer(args):
    board = load_board()
    if board is None:
        print("questions: unknown · the shared answer-wait observation is not available yet",
              file=sys.stderr)
        return 3
    item, error = _find(board, args.id)
    if item is None:
        print("questions: " + error, file=sys.stderr)
        return 2
    if not (item.get("owner") or {}).get("harness"):
        print("questions: %s has no exact owner session binding" % item["id"], file=sys.stderr)
        return 2
    envelope = HWS.read_selected_request(_reference(item))
    support = envelope.get("support") if isinstance(envelope.get("support"), dict) else {}
    if envelope.get("state") != "open" or support.get("supported") is not True:
        reason = support.get("reason") or envelope.get("reason") or envelope.get("state")
        print("questions: %s cannot be answered here (%s)" % (item["id"], reason), file=sys.stderr)
        return 2
    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    answers = None
    if args.answers_file:
        try:
            answers = json.loads(Path(args.answers_file).read_text(encoding="utf-8"))
        except ValueError:
            print("questions: --answers-file is not JSON", file=sys.stderr)
            return 1
    response, error = build_response(envelope, choices=args.choice or (), text=text,
                                     decision=args.decision, answers=answers)
    if response is None:
        print("questions: " + error, file=sys.stderr)
        return 1
    ref = dict(envelope)
    ref["id"] = item["id"]
    listed = questions_of(envelope)
    if len(listed) == 1:
        ref.setdefault("question_id", listed[0]["id"])
    result = HWS.submit_selected_response(ref, response)
    state = result.get("state") if isinstance(result, dict) else "unknown"
    outcome = result.get("result") if isinstance(result, dict) else None
    reason = outcome.get("reason") if isinstance(outcome, dict) else None
    reason = reason or (result.get("reason") if isinstance(result, dict) else None)
    if args.json:
        print(json.dumps({"id": item["id"], "state": state, "reason": reason},
                         ensure_ascii=False))
    else:
        print("questions: %s %s%s" % (item["id"], state, " (%s)" % reason if reason else ""))
    return 0 if state in _DELIVERED else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hearting run questions", allow_abbrev=False,
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)
    listing = sub.add_parser("list", allow_abbrev=False, help="list waiting questions (default)")
    answer = sub.add_parser("answer", allow_abbrev=False,
                            help="send the person's chosen answer the existing way")
    for target, default in ((parser, False), (listing, argparse.SUPPRESS)):
        target.add_argument("--json", action="store_true", default=default)
        target.add_argument("--all", action="store_true", default=default,
                            help="show question detail for older and ownerless waits too")
    answer.add_argument("id", help="question id from the list (a unique prefix is enough)")
    picked = answer.add_mutually_exclusive_group(required=True)
    picked.add_argument("--choice", type=int, action="append",
                        help="1-based choice number; repeat once per question in list order")
    picked.add_argument("--text", help="free-text answer for a single question")
    picked.add_argument("--text-file", help="file holding the free-text answer")
    picked.add_argument("--decision", choices=("approve", "reject", "defer"))
    picked.add_argument("--answers-file", help="JSON object: question id -> answer(s)")
    answer.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "answer":
        return _answer(args)
    return _list(args)


if __name__ == "__main__":
    raise SystemExit(main())
