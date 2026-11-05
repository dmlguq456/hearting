#!/usr/bin/env python3
"""The person's reply to a native question tool, kept for the registered frame question.

Claude `AskUserQuestion`, Codex `request_user_input` and OpenCode `question` return the
person's choices when the tool call ends; each harness's post-tool hook passes that payload
here (`--claude`, `--codex`, `--opencode`). When one of this session's routes has its
frame-review gate waiting on a registered question that the reply answers, the reply is kept
beside the question (`work_start.record_native_answer`) and the next `start` takes it, so the
caller does not copy the answer into a file. The replies of one interview may span several
calls (a tool shows only a few questions at once): the rows so far wait in a partial file
beside the question until every registered question has an answer. Anything else is left
alone; this never blocks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

NOTE_PREFIX = "user_note: "          # Codex TUI: text typed beside or instead of an option


def _questions(value) -> list:
    return [q for q in value if isinstance(q, dict)] if isinstance(value, list) else []


def _labels(question) -> list:
    return [str(o.get("label") or "") for o in question.get("options") or [] if isinstance(o, dict)]


def asked_from(harness: str, payload: dict) -> tuple[str, list]:
    """`(session id, [{question, options, answer}])` of one native question call."""
    if harness == "claude":
        if payload.get("tool_name") != "AskUserQuestion":
            return "", []
        response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
        replies = response.get("answers") if isinstance(response.get("answers"), dict) else {}
        questions = _questions((payload.get("tool_input") or {}).get("questions"))
        return str(payload.get("session_id") or ""), [
            {"question": q.get("question"), "options": _labels(q), "answer": str(replies.get(q.get("question")) or "")}
            for q in questions]
    if harness == "codex":
        if payload.get("tool_name") != "request_user_input":
            return "", []
        response = payload.get("tool_response")
        try:
            response = json.loads(response) if isinstance(response, str) else response
        except ValueError:
            return "", []
        replies = (response or {}).get("answers") if isinstance(response, dict) else {}
        asked = []
        for q in _questions((payload.get("tool_input") or {}).get("questions")):
            given = ((replies or {}).get(q.get("id")) or {}).get("answers") or []
            picked = [str(a) for a in given if not str(a).startswith(NOTE_PREFIX)]
            typed = [str(a)[len(NOTE_PREFIX):] for a in given if str(a).startswith(NOTE_PREFIX)]
            row = {"question": q.get("question"), "options": _labels(q),
                   "answer": picked[0] if picked else (typed[0] if typed else "")}
            if picked and typed:
                row["note"] = typed[0]   # the words the person added beside the option they picked
            asked.append(row)
        return str(payload.get("session_id") or ""), asked
    if harness == "opencode":
        if payload.get("tool") != "question":
            return "", []
        replies = payload.get("answers") if isinstance(payload.get("answers"), list) else []
        asked = []
        for index, q in enumerate(_questions((payload.get("args") or {}).get("questions"))):
            given = replies[index] if index < len(replies) and isinstance(replies[index], list) else []
            asked.append({"question": q.get("question"), "options": _labels(q), "answer": ", ".join(map(str, given))})
        return str(payload.get("sessionID") or ""), asked
    return "", []


def _session_routes(harness: str, session_id: str) -> list:
    """This session's route files from its route-chain ledger, newest first."""
    tools = ROOT / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    from fleet import route_chain
    found = []
    for line in reversed(route_chain.read_tail(harness, session_id, max_bytes=None)):
        if line["route_file"] not in found:
            found.append(line["route_file"])
    return found


def record(harness: str, payload: dict) -> Path | None:
    session_id, asked = asked_from(harness, payload)
    if not session_id or not any(row.get("answer") for row in asked):
        return None
    import work_start
    from dispatch_contract import stable_state_root
    jobs = Path(os.environ.get("AGENT_DISPATCH_JOBS") or stable_state_root(os.environ) / "jobs.log")
    candidates = []
    for route_file in _session_routes(harness, session_id):
        try:
            from fleet import route as route_reader
            route = route_reader.load(route_file)
            if route is None:
                continue
            candidate = work_start.native_answer_candidate(route, jobs, asked)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if candidate is not None:
            candidates.append((route, candidate))
    if len(candidates) != 1:
        return None
    route, (_target, _answers) = candidates[0]
    try:
        # Recompute immediately before the existing write path so a changed raise,
        # route, or sibling answer cannot inherit an earlier candidate decision.
        return work_start.record_native_answer(route, jobs, asked)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    harness = next((flag[2:] for flag in argv if flag in ("--claude", "--codex", "--opencode")), "")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        kept = record(harness, payload) if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 -- a reply that cannot be kept is answered by hand as before
        return 0
    if kept is not None and harness in ("claude", "codex"):
        import frame_interview as FI
        if kept.name == FI.NATIVE_PARTIAL_NAME:
            try:
                remaining = len(json.loads(kept.read_text(encoding="utf-8"))["missing"])
            except (OSError, ValueError, KeyError, TypeError):
                return 0
            context = (f"The person's answers so far are kept beside the registered frame question; {remaining} "
                       "registered question(s) remain. Ask them verbatim in the next native question call; "
                       "the runtime combines the calls.")
        else:
            context = (f"The person's answer to the registered frame question is kept at {kept}. "
                       "Run resume_command (or a bare `capability-route start`) to continue; "
                       "no answers file is needed.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
