#!/usr/bin/env python3
"""The person's intent at the start of a route, kept beside it (WORKFLOW §0.4).

A route used to carry only the agent-written request (`work_request.text`). The
person's own words and the one-line goal it was cut from reached no owner or
worker unless a frame-review answer happened to sit on that same route. Two
values now travel with every route, neither of them part of its identity:

- `intent`, `scope`, `not_doing`, `done`: the agent's leading `의도:` / `범위:` /
  `안 할 것:` / `완료:` lines (or `Intent:` / `Scope:` / `Not doing:` / `Done:`)
  of the prompt file, read again from the sealed `work_request.text` whenever
  needed. With no intent line the request's first sentence is a marked draft.
- `original`: a short excerpt of the person's own prompt, read by the runtime
  from the session record when a route is first written -- only at a person's
  prompt boundary (not a peer or runtime transfer), skipping bare approvals such
  as "진행", with credentials masked -- and stored once in
  `.runtime/intent/<work key>.json` beside the route. Every leg of a framed route
  shares the frame route's record and never reads the session itself.

Every value is optional and every failure leaves only that value out: this is a
record and a carrier, never a gate or a required input.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MAX_ORIGINAL_CHARS = 600
MAX_LINE_CHARS = 300
MAX_NOT_DOING = 3
MAX_APPROVALS_SKIPPED = 6
TAIL_BYTES = 1024 * 1024
RECORD_SCHEMA = "work_intent_original_v1"
_LABELS = {
    "intent": ("의도", "intent", "goal"),
    "scope": ("범위", "scope"),
    "not_doing": ("안 할 것", "하지 않을 것", "not doing", "non-goals", "out of scope"),
    "done": ("완료", "done", "done when"),
}
_HEADER = re.compile(r"^\s*(?:[-*]\s+)?(?P<label>[^:：]{1,28})\s*[:：]\s*(?P<value>.*\S)?\s*$")
_NOT_HUMAN_PREFIXES = ("<environment_context", "<user_instructions", "<turn_aborted", "<user_shell_command")
# A bare answer to a card or question is not the request; the person's request is the prompt before it.
_APPROVAL = re.compile(
    r"^\s*(?:진행(?:해|해요|하자|해\s*줘|해\s*주세요)?|계속(?:해)?|네|예|응|ㅇㅇ|좋아(?:요)?|그래|오케이"
    r"|ok(?:ay)?|yes|y|proceed|go(?:\s+ahead)?|continue|lgtm)\s*(?:[(（](?:권장|recommended)[)）])?\s*[.!~]*\s*$",
    re.I)
# Credentials a pasted prompt may carry. URL userinfo and Bearer values are masked by
# `attempt_view.sanitize`; `sk-` keys as in `codex_parent_completion_replay._stderr_summary`;
# the token prefixes as in `tools/install/extensions.py` SECRET_PATTERNS.
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,255}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
)
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b((?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*)[^\s,;]{6,}")


def _line(value, cap=MAX_LINE_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _label(raw: str) -> str | None:
    key = " ".join(raw.replace("*", "").strip().lower().split())
    return next((name for name, labels in _LABELS.items() if key in labels), None)


def _split_header(text: str) -> tuple[dict, str]:
    """The leading label lines of a prompt file and the text after them."""
    found: dict = {}
    lines = (text or "").splitlines()
    index = 0
    for index, raw in enumerate(lines):
        if not raw.strip():
            if found:
                break
            continue
        match = _HEADER.match(raw)
        name = _label(match["label"]) if match else None
        if name is None:
            break
        value = (match["value"] or "").replace("**", "").strip()
        if not value:
            continue
        if name == "not_doing":
            items = [_line(part) for part in re.split(r"\s*[;；]\s*", value) if part.strip()]
            found["not_doing"] = (found.get("not_doing", []) + items)[:MAX_NOT_DOING]
        elif name not in found:
            found[name] = _line(value)
    else:
        index = len(lines)
    return found, "\n".join(lines[index:]) if found else (text or "")


def parse_header(text: str) -> dict:
    return _split_header(text)[0]


def draft(text: str) -> dict:
    """Intent fields from the prompt file alone: its header, else its first sentence."""
    found, body = _split_header(text)
    if found.get("intent"):
        return {**found, "basis": "header"}
    import frame_interview
    first = frame_interview.understanding_draft(body)
    if first:
        found = {**found, "intent": _line(first), "basis": "first-sentence"}
    return found


def redact_secrets(text: str) -> str:
    import attempt_view
    value = attempt_view.sanitize(text or "")
    for pattern in _SECRETS:
        value = pattern.sub("[redacted]", value)
    return _SECRET_ASSIGNMENT.sub(r"\1[redacted]", value)


# ---------------------------------------------------------------------------
# The person's words, read from the session record at the current prompt
# ---------------------------------------------------------------------------

def _human_prompt_boundary(harness: str, sid: str, cwd, environ) -> bool:
    """The current prompt boundary exists and carries no peer/runtime transfer origin."""
    import session_tidy
    seat = session_tidy.resolve_seat(harness, cwd or None, environ, sid, "")
    turn = session_tidy.read_json(session_tidy._prompt_seq_path(seat))
    return (isinstance(turn, dict) and turn.get("sid") == sid and turn.get("harness") == harness
            and type(turn.get("seq")) is int and not turn.get("request_origin"))


def _human_text(text: str) -> str:
    from fleet import refresh_title
    value = refresh_title._human_language_text(text or "")
    if not value or value.lstrip().startswith(_NOT_HUMAN_PREFIXES):
        return ""
    return value.strip()


def _first_request(texts) -> str:
    """From the newest user text back: skip bare approvals, stop at the first request.

    The newest text decides whose boundary this is: an envelope there is not the person's."""
    skipped = 0
    for raw in texts:
        text = _human_text(raw)
        if not text:
            return ""
        if not _APPROVAL.match(text):
            return text
        skipped += 1
        if skipped > MAX_APPROVALS_SKIPPED:
            return ""
    return ""


def _jsonl_user_texts(harness: str, path: Path):
    import tidy_transcripts
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - TAIL_BYTES)
        handle.seek(start)
        if start:
            handle.readline()
        rows = handle.read().splitlines()
    for raw in reversed(rows):
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        role, text = tidy_transcripts._text_of_jsonl_row(data, harness)
        if role == "user":
            yield text


def _opencode_user_texts(db_path: Path, sid: str):
    import tidy_transcripts
    from fleet import refresh_title
    with tidy_transcripts._opencode_view(db_path) as con:
        rows = con.execute(
            "SELECT p.data, m.data FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? ORDER BY p.rowid DESC LIMIT 400", (sid,)).fetchall()
    for raw, message_raw in rows:
        try:
            part, message = json.loads(raw), json.loads(message_raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if (not isinstance(part, dict) or part.get("type") != "text"
                or part.get("synthetic") or part.get("ignored")):
            continue
        yield refresh_title._opencode_text(part)


def original_excerpt(cwd=None, environ=None) -> str:
    """The person's request at the current prompt, bounded and masked; empty whenever unsure."""
    environ = os.environ if environ is None else environ
    try:
        from session_identity import identity
        who = identity(environ)
        if not who.known or not who.session_id or who.harness not in ("claude", "codex", "opencode"):
            return ""
        if not _human_prompt_boundary(who.harness, who.session_id, cwd, environ):
            return ""
        tools = HERE.parent / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import tidy_transcripts
        source = tidy_transcripts.locate_transcript(who.harness, who.session_id)
        if source is None:
            return ""
        texts = (_opencode_user_texts(Path(source), who.session_id) if who.harness == "opencode"
                 else _jsonl_user_texts(who.harness, Path(source)))
        text = redact_secrets(_first_request(texts)).strip()
    except Exception:  # noqa: BLE001 -- the excerpt is optional; any failure leaves it out
        return ""
    if len(text) > MAX_ORIGINAL_CHARS:
        text = text[:MAX_ORIGINAL_CHARS - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# The record beside the route: written once, outside the route's identity
# ---------------------------------------------------------------------------

def work_key(route) -> str | None:
    """The route whose start the person's words belong to: a leg's frame route, else itself."""
    plan = route.get("route_plan") if isinstance(route, dict) else None
    if plan is None:
        return route.get("route_id") if isinstance(route, dict) else None
    try:
        record = json.loads((Path(route["artifact_root"]) / plan["decision"]).read_text(encoding="utf-8"))
        key = record["decision"]["frame_route"]["route_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return key if isinstance(key, str) and re.fullmatch(r"rt-[0-9a-f]{16}", key) else None


def record_path(route) -> Path | None:
    key = work_key(route)
    root = route.get("artifact_root") if isinstance(route, dict) else None
    if not key or not isinstance(root, str) or not re.fullmatch(r"rt-[0-9a-f]{16}", key):
        return None
    return Path(root) / ".runtime" / "intent" / f"{key}.json"


def seal_original(route, cwd=None, environ=None) -> str:
    """Write the excerpt once for a route the person started; a leg or a repeat keeps the first."""
    if not isinstance(route, dict) or not isinstance(route.get("work_request"), dict) or route.get("route_plan"):
        return ""
    path = record_path(route)
    if path is None or path.exists():
        return ""
    original = original_excerpt(cwd or route.get("cwd"), environ)
    if not original:
        return ""
    body = json.dumps({"schema": RECORD_SCHEMA, "route_id": route["route_id"], "original": original,
                       "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      ensure_ascii=False, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return ""
    except OSError:
        return ""
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return original


def read_original(route) -> str:
    path = record_path(route)
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path is not None else None
    except (OSError, ValueError):
        return ""
    text = value.get("original") if isinstance(value, dict) and value.get("schema") == RECORD_SCHEMA else ""
    return text if isinstance(text, str) and len(text) <= MAX_ORIGINAL_CHARS else ""


# ---------------------------------------------------------------------------
# Readers: the compose card and the owner/worker prompt
# ---------------------------------------------------------------------------

def route_intent(route, *, original=None) -> dict:
    """What the route says the person wants: the prompt file's lines and the person's words."""
    request = route.get("work_request") if isinstance(route, dict) else None
    value = draft(request.get("text") or "") if isinstance(request, dict) else {}
    words = read_original(route) if original is None else original
    return {"original": words, **value} if words else value


# The card labels when no translator is given; the same text as `route.intent.*` in
# tools/i18n/messages.json.
_KO_LABELS = {
    "route.intent.line": "[의도] {intent}",
    "route.intent.drafted": " (요청 첫 문장)",
    "route.intent.scope": "범위 {scope}",
    "route.intent.not_doing": "안 할 것 {items}",
    "route.intent.done": "완료 {done}",
    "route.intent.original": "원문 \"{original}\"",
}


def card_lines(route, *, original=None, t=None) -> list[str]:
    """Two or three lines under compose's `[경로]` notice; `t` is the route card's UI-text
    translator (Korean labels without one). The person's words and the intent stay as written."""
    value = route_intent(route, original=original)
    if not value.get("intent") and not value.get("original"):
        return []
    label = t or (lambda key, **fields: _KO_LABELS[key].format(**fields))
    lines = []
    if value.get("intent"):
        drafted = label("route.intent.drafted") if value.get("basis") == "first-sentence" else ""
        lines.append("  " + label("route.intent.line", intent=_line(value["intent"], 160)) + drafted)
    extra = []
    if value.get("scope"):
        extra.append(label("route.intent.scope", scope=_line(value["scope"], 80)))
    if value.get("not_doing"):
        extra.append(label("route.intent.not_doing", items="; ".join(_line(item, 80) for item in value["not_doing"])))
    if value.get("done"):
        extra.append(label("route.intent.done", done=_line(value["done"], 100)))
    if extra:
        lines.append("    " + " · ".join(extra))
    if value.get("original"):
        lines.append("    " + label("route.intent.original", original=_line(value["original"], 120)))
    return lines


def prompt_block(route) -> str:
    """The route's intent as an owner/worker prompt section; empty when there is none."""
    value = route_intent(route)
    if not value.get("intent") and not value.get("original"):
        return ""
    lines = ["Intent at the start of this work (scope reference, not a new task):"]
    if value.get("original"):
        lines.append("- Person's words (excerpt): " + json.dumps(value["original"], ensure_ascii=False))
    if value.get("intent"):
        drafted = " (drafted from the request's first sentence)" if value.get("basis") == "first-sentence" else ""
        lines.append(f"- Intent: {value['intent']}{drafted}")
    if value.get("scope"):
        lines.append(f"- Scope: {value['scope']}")
    for item in value.get("not_doing") or []:
        lines.append(f"- Not doing: {item}")
    if value.get("done"):
        lines.append(f"- Done when: {value['done']}")
    lines.append("Stay within this intent; the assignment below says which part is yours.")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# The end: one mechanical completion summary, sealed with the owner envelope
# ---------------------------------------------------------------------------

SUMMARY_NAME = "completion-summary.md"
SUMMARY_HEAD = "[완료 요약]"
MAX_FIELD_CHARS = 320
MAX_SUMMARY_CHARS = 1600
_SECTION = re.compile(r"^#{2,3}\s+(?P<title>.+?)\s*$")
_NUMBER = re.compile(r"^(?P<num>\d+(?:\.\d+)?)[.)]?\s")
# (field, sections of the code-report template by number, words of any other heading). The numbers
# hold only for a code report (`code-report` writes `## 1. Change Overview` … `## 6. Follow-Ups`);
# other documents number their own sections, so their headings are read by words only.
_SECTION_FIELDS = (
    ("remaining", ("5", "6"), ("미검증", "실패", "건너뛴", "건너뜀", "남은", "후속", "한계", "unverified", "failed",
                               "skipped", "follow-up", "follow up", "follow-ups", "remaining", "limitation", "limits",
                               "open risk")),
    ("verified", ("4",), ("검증", "시험", "qa", "verification", "tests", "testing")),
    ("changed", ("1",), ("변경 개요", "변경 사항", "변경 내용", "바뀐 것", "고친 것", "고친 행동", "사용자용",
                         "change overview", "changes", "what changed")),
)
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_CARRIER_MARKERS = ("HEARTING_RECEIPT_V1", "AGENT_HARNESS_COMPLETION_V1")


def _has_word(title: str, word: str) -> bool:
    if word.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", title) is not None
    return word in title


def _section_field(title: str, *, numbered: bool) -> str | None:
    number = _NUMBER.match(title)
    if number and numbered:
        return next((field for field, numbers, _words in _SECTION_FIELDS if number["num"] in numbers), None)
    lowered = (title[number.end():] if number else title).lower()
    return next((field for field, _numbers, words in _SECTION_FIELDS
                 if any(_has_word(lowered, word) for word in words)), None)


def report_sections(text: str, *, numbered: bool = False) -> dict[str, list[str]]:
    """The report's own lines under its overview, verification and remaining-work headings.

    `numbered` reads `1`/`4`/`5`/`6` as the code-report template's sections; any other heading
    is matched by its words."""
    sections: dict[str, list[str]] = {}
    current = None
    for raw in (text or "").splitlines():
        match = _SECTION.match(raw)
        if match:
            current = _section_field(match["title"].strip(), numbered=numbered)
            continue
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", raw).strip()
        if (current and line and not re.fullmatch(r"[|:\-\s]+", line)
                and not any(marker in line for marker in _CARRIER_MARKERS)):
            sections.setdefault(current, []).append(line.strip("|").strip())
    return sections


def _joined(lines, cap=MAX_FIELD_CHARS) -> str:
    return _line(" · ".join(item for item in lines if item), cap)


def _review_degraded(route_file) -> list[str]:
    if not route_file:
        return []
    path = Path(route_file)
    try:
        outcome = json.loads(path.with_name(path.stem + ".outcome.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    nodes = outcome.get("review_independence_degraded") if isinstance(outcome, dict) else None
    return [str(node) for node in nodes] if isinstance(nodes, list) else []


def _result_line(route) -> str:
    """Completed; for one leg of an approved plan, say that a later leg remains."""
    plan = route.get("route_plan")
    if isinstance(plan, dict):
        try:
            record = json.loads((Path(route["artifact_root"]) / plan["decision"]).read_text(encoding="utf-8"))
            legs = len(record["decision"]["proposal"]["legs"])
            index = int(plan["index"])
        except (OSError, ValueError, KeyError, TypeError):
            legs = index = None
        if legs is not None and index + 1 < legs:
            return f"이 leg({index + 1}/{legs}) 완료 — 다음 leg가 남음"
    return "완료 — 런타임이 route·사이클 종결을 확인함"


def completion_summary(route, primary, *, route_file=None) -> str:
    """The runtime's close text for one completed owner: intent, result, changes, checks, the rest.

    Every value comes from the sealed route, the route outcome and the primary
    report itself; a report whose headings cannot be matched is named, not
    summarised, so nothing is invented and nothing unverified reads as verified.
    """
    request = route.get("work_request") if isinstance(route.get("work_request"), dict) else {}
    intent = route_intent(route) or {}
    head = f"{SUMMARY_HEAD} {route.get('capability') or '-'} · route {route.get('route_id') or '-'}"
    lines = [head]
    goal = intent.get("intent") or ""
    if not goal and request.get("text"):
        goal = draft(request["text"]).get("intent") or ""
    if goal:
        original = f" (원문 \"{_line(intent['original'], 100)}\")" if intent.get("original") else ""
        lines.append(f"의도: {_line(goal, 200)}{original}")
    lines.append("결과: " + _result_line(route))
    try:
        text = Path(primary).read_text(encoding="utf-8") if primary else ""
    except (OSError, UnicodeError):
        text = ""
    code_report = route.get("capability") == "autopilot-code" or Path(str(primary or "")).name == "final_report.md"
    sections = report_sections(text, numbered=code_report)
    degraded = _review_degraded(route_file)
    if sections:
        if sections.get("changed"):
            lines.append("바뀐 것: " + _joined(sections["changed"]))
        checks = _joined(sections.get("verified") or []) or "보고서에 검증 절이 없음"
        lines.append("검증: " + checks)
        lines.append("미검증·남은 것: " + (_joined(sections.get("remaining") or [])
                                         or "보고서에 따로 적힌 것 없음"))
    else:
        lines.append("검증·남은 것: 보고서 형식이 달라 기계 요약 안 함 — 보고서 본문을 그대로 볼 것")
    if degraded:
        lines.append("독립 리뷰 없이 닫힌 노드: " + ", ".join(degraded))
    prs = list(dict.fromkeys(_PR_URL.findall(text)))[:3]
    artifact = f"산출물: {primary}" if primary else "산출물: 없음"
    lines.append(artifact + (" · PR " + " ".join(prs) if prs else ""))
    rendered = "\n".join(line for line in lines if line.strip())
    return rendered[:MAX_SUMMARY_CHARS].rstrip() + "\n"


def sealed_summary(metadata, attempt_id: str) -> str:
    """The summary the runtime sealed for this owner attempt; empty when there is none."""
    try:
        route_file = metadata.get("owner_route_file") or metadata.get("route_file")
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        from dispatch_terminal_commit import terminal_slot
        path = terminal_slot(Path(route["artifact_root"]), route["route_id"], attempt_id) / SUMMARY_NAME
        text = path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 -- a missing summary leaves the existing delivery text as it was
        return ""
    if not text.startswith(SUMMARY_HEAD) or "\n\n" in text or any(m in text for m in _CARRIER_MARKERS):
        return ""
    return text
