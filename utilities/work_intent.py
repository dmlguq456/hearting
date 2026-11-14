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
- `original`: the person's words explicitly quoted in this route's task, with
  credentials masked. A recent session utterance alone cannot establish that
  it belongs to this task. The optional quotation is stored once in
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


def original_work_request(text: str) -> str:
    """Unwrap an agreed-intent-first leg before displaying or clipping its request."""
    # Leg prompts put the agreed answers first. The original request still owns
    # these display/intent fields; headings in the generated wrapper are not goals.
    if (text or "").startswith("## Agreed intent\n"):
        _, separator, original = text.partition("\n## Original work request\n\n")
        if separator and "\n## Frame briefs (read-only input)\n" in original:
            return original.rsplit("\n## Frame briefs (read-only input)\n", 1)[0].strip()
    return text or ""


def _split_header(text: str) -> tuple[dict, str]:
    """The leading label lines of a prompt file and the text after them."""
    text = original_work_request(text)
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
        # The shared prompt hook captures this boundary before later transcript
        # writes can move it. Older boundaries without a quote stay unknown.
        import session_tidy
        seat = session_tidy.resolve_seat(who.harness, cwd or None, environ, who.session_id, "")
        turn = session_tidy.read_json(session_tidy._prompt_seq_path(seat))
        text = turn.get("request_excerpt", "")
        if not isinstance(text, str):
            return ""
        text = redact_secrets(text).strip()
    except Exception:  # noqa: BLE001 -- the excerpt is optional; any failure leaves it out
        return ""
    if len(text) > MAX_ORIGINAL_CHARS:
        text = text[:MAX_ORIGINAL_CHARS - 1].rstrip() + "…"
    return text


def prompt_excerpt(text) -> str:
    """A bounded quote of this exact human boundary, never an earlier request."""
    text = _human_text(text) if isinstance(text, str) else ""
    if not text or _APPROVAL.fullmatch(text):
        return ""
    text = redact_secrets(text)
    return text if len(text) <= MAX_ORIGINAL_CHARS else text[:MAX_ORIGINAL_CHARS - 1].rstrip() + "…"


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


def frame_reference(route):
    """The exact frame identity/root, also for a leg with an absolute plan reference."""
    import route_plan as RP
    if RP.is_framed_route(route):
        return Path(route["artifact_root"]), route["route_id"], route["route_hash"]
    try:
        plan = route["route_plan"]
        binding = RP.read_route_plan(f"{plan['decision']}#{plan['index']}", route["artifact_root"])
        if binding["digest"] != plan["digest"]:
            return None
        decision = binding["record"]["decision"]
        return (Path(decision["first_leg_compose"]["context"]["artifact_root"]),
                decision["frame_route"]["route_id"], decision["frame_route"]["route_hash"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


FRAME_CORRECTION_INPUT_BYTES = 65536
FRAME_CORRECTION_DELIVERY_BYTES = 8192


def frame_correction_view(route):
    """Retained history and bounded newest-first delivery share one admission contract.

    The writer retains full accepted inputs. Total file size is not a reason to
    silently discard them; only delivery is shortened, with explicit coverage.
    """
    ref = frame_reference(route)
    empty = {"requests": [], "total": 0, "omitted": 0, "truncated": False, "reference": ""}
    if ref is None:
        return empty
    root, rid, rhash = ref
    path = root / ".runtime/framed-decision" / (rid + ".corrections.json")
    try:
        if path.is_symlink():
            return empty
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if (value.get("schema"), value.get("route_id"), value.get("route_hash")) != (
                "frame_route_input_v1", rid, rhash):
            return empty
        retained = [item for item in value["requests"] if isinstance(item, dict)
                    and isinstance(item.get("id"), str) and isinstance(item.get("text"), str)]
    except (OSError, ValueError, KeyError, TypeError):
        return empty
    items, remaining, shortened = [], FRAME_CORRECTION_DELIVERY_BYTES, False
    for item in reversed(retained):
        overhead = len(item["id"].encode("utf-8")) + 32
        if remaining <= overhead:
            break
        raw = item["text"].encode("utf-8")
        text = raw[:remaining-overhead].decode("utf-8", errors="ignore")
        shortened |= len(text.encode("utf-8")) < len(raw)
        items.append({**item, "text": text})
        remaining -= overhead + len(text.encode("utf-8"))
        if shortened:
            break
    omitted = len(retained) - len(items)
    return {"requests": items, "total": len(retained), "omitted": omitted,
            "truncated": shortened or omitted > 0, "reference": str(path)}


def frame_corrections(route):
    return frame_correction_view(route)["requests"]


def frame_correction_coverage(view):
    return {"total": view["total"], "included": len(view["requests"]), "omitted": view["omitted"],
            "truncated": view["truncated"], "reference": view["reference"]}


def frame_corrections_text(route):
    view = frame_correction_view(route)
    if not view["requests"]:
        return ""
    coverage = (f"Coverage: truncated; recent conditions first, {len(view['requests'])}/{view['total']} "
                f"entries shown ({view['omitted']} omitted; displayed text may be shortened). "
                f"Full retained history: {view['reference']}\n\n") if view["truncated"] else ""
    return ("\n## Conditions added during framing\n\n" + coverage + "\n".join(
        f"- [{item['id']}] {item['text']}" for item in view["requests"]) + "\n")


def task_quotation(text) -> str:
    """Only attributed quotations already present in the assignment.

    No semantic guessing from the latest conversation and no required format:
    unfamiliar attribution simply has no optional quote.
    """
    if not isinstance(text, str):
        return ""
    attribution = re.compile(
        r'^\s*(?:#{1,6}\s+)?(?:[-*]\s+)?(?:사용자(?:\s*원문)?|원문|user(?:\s+(?:said|quote))?|original)'
        r'(?:\s*[（(][^\n)）]{0,40}[)）])?\s*[:：]\s*(.*)$', re.I)
    quoted = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|「([^」\n]+)」')
    for raw in text.splitlines():
        match = attribution.match(raw.replace('**', ''))
        if not match:
            continue
        values = [next(value for value in item if value) for item in quoted.findall(match[1])]
        if values:
            return _line(redact_secrets(" · ".join(values)), MAX_ORIGINAL_CHARS)
    return ""


def _task_quotation(route) -> str:
    if not isinstance(route, dict):
        return ""
    if route.get("route_plan"):
        reference = frame_reference(route)
        if reference is None:
            return ""
        root, rid, digest = reference
        try:
            frame = json.loads((root / ".runtime/routes" / (rid + ".json")).read_text())
            from route_identity import route_hash
            if frame.get("route_id") != rid or frame.get("route_hash") != digest or route_hash(frame) != digest:
                return ""
            route = frame
        except (OSError, ValueError, TypeError):
            return ""
    request = route.get("work_request")
    return task_quotation(request.get("text")) if isinstance(request, dict) else ""


def seal_original(route, cwd=None, environ=None) -> str:
    """Write the excerpt once for a route the person started; a leg or a repeat keeps the first."""
    if not isinstance(route, dict) or not isinstance(route.get("work_request"), dict) or route.get("route_plan"):
        return ""
    path = record_path(route)
    if path is None or path.exists():
        return ""
    original = _task_quotation(route)
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
    # Keep old records untouched, but do not display their unbound session quote.
    return text if isinstance(text, str) and text == _task_quotation(route) else ""


# ---------------------------------------------------------------------------
# Readers: the compose card and the owner/worker prompt
# ---------------------------------------------------------------------------

def route_intent(route, *, original=None) -> dict:
    """What the route says the person wants: the prompt file's lines and the person's words."""
    request = route.get("work_request") if isinstance(route, dict) else None
    value = draft(request.get("text") or "") if isinstance(request, dict) else {}
    # `original` remains a compatible call parameter, never independent proof.
    words = _task_quotation(route)
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
    return "\n".join(lines) + "\n\n" + frame_corrections_text(route)


# ---------------------------------------------------------------------------
# The end: one mechanical completion summary, sealed with the owner envelope
# ---------------------------------------------------------------------------

SUMMARY_NAME = "completion-summary.md"
MAX_FIELD_CHARS = 320
MAX_SUMMARY_CHARS = 1600
_SECTION = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_BOLD_FIELD = re.compile(r"^\s*\*\*(?P<title>[^*]+?)\*\*[:：]?\s*(?P<value>.*)$")
_NUMBER = re.compile(r"^(?P<num>\d+(?:\.\d+)?)[.)]?\s")
# (field, sections of the code-report template by number, words of any other heading). The numbers
# hold only for a code report (`code-report` writes `## 1. Change Overview` … `## 6. Follow-Ups`);
# other documents number their own sections, so their headings are read by words only.
_SECTION_FIELDS = (
    ("remaining", ("5",), ("미검증", "실패", "건너뛴", "건너뜀", "남은", "해결하지", "필수", "unverified", "failed",
                           "skipped", "remaining", "unresolved")),
    # Limits, residual risks and follow-ups are shown with the rest, but only a failed/skipped,
    # unverified or remaining-work heading (and the checks) can say that something failed.
    ("limits", (), ("한계", "limitation", "limits", "open risk")),
    # "하지 않은 것" lists what the work left to others on purpose (install, merge): follow-ups.
    ("follow_up", ("6",), ("후속", "하지", "follow-up", "follow up", "follow-ups", "not done")),
    ("verified", ("4",), ("검증", "시험", "검사", "명령", "qa", "verification", "validation", "checks", "tests",
                          "testing")),
    ("changed", ("1",), ("변경 개요", "변경 사항", "변경 내용", "수정 사항", "수정 내용", "변경 파일", "바뀐 것", "고친 것", "고친 행동", "사용자용",
                         "change overview", "changes", "what changed")),
)
_FAILURE_HEADINGS = ("미검증", "실패", "건너뛴", "건너뜀", "해결하지", "unverified", "failed", "skipped",
                     "unresolved")
# The report's own first line when it states the verdict (`최종 판정: …`, `Final verdict: …`).
_VERDICT_LINE = re.compile(r"^\s*(?:[-*]\s+)?\**\s*(?:최종\s*판정|판정|결론|(?:final\s+)?verdict)\s*\**\s*[:：]\s*\**\s*"
                           r"(?P<verdict>.+?)\s*$", re.I)
# A line naming the publication: a PR link or number with its head, CI or mergeability.
_PUBLICATION_LINE = re.compile(r"pull/(?P<url>\d+)|\bPR\s*#\s*(?P<number>\d+)", re.I)
_PUBLICATION_WORDS = re.compile(r"\bhead\b|\bCI\b|mergeable|\bsha\b", re.I)
_PUBLICATION_FIELDS = (
    re.compile(r"\bhead\W{0,3}(?P<value>[0-9a-f]{7,40})\b", re.I),
    re.compile(r"\bCI\W{0,3}(?P<value>\d+\s*/\s*\d+(?:\s*[가-힣A-Za-z]+)?|[A-Za-z가-힣]+)", re.I),
    re.compile(r"(?P<value>\b(?:MERGEABLE|CONFLICTING|UNKNOWN)\b|병합\s*가능)"),
)
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_CARRIER_MARKERS = ("HEARTING_RECEIPT_V1", "AGENT_HARNESS_COMPLETION_V1")
_UNRUN_WORDS = (r"not (?:re[- ]?)?run|not exercised|unverified|미실행|미확인|미검증|중단|돌리지 않|실행하지 않|재실행 안"
                r"|돌리지 못|실행하지 못|못 돌|실행 못|could not (?:be )?run|couldn['’]t (?:be )?run|unable to run")
_UNRUN_CHECK = re.compile(
    r"(?:\b(?:test|tests|suite|CI|live)\b|시험|검사|검증|실기).{0,160}(?:" + _UNRUN_WORDS + ")", re.I)
# The verification section's not-run wording is _UNRUN_CHECK's alone to move; a line it left
# in place ("통과해서 다시 실행하지 않았다") is not read again as a failure.
_UNRUN_WORD = re.compile(_UNRUN_WORDS, re.I)


def _ui_text():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "tools" / "i18n" / "ui_text.py"
    spec = importlib.util.spec_from_file_location("ui_text", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ui_translator():
    """The shared UI-text translator (English by default, Korean when declared)."""
    return _ui_text().translator()


def summary_heads() -> tuple[str, ...]:
    """The summary's first words in every catalog language; the writer chose the language."""
    module = _ui_text()
    return tuple(module.text("completion.head", lang) for lang in module.LANGUAGES)


def _has_word(title: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9가-힣])" + re.escape(word)
                     + r"(?=$|[^a-z0-9가-힣]|[과와](?:\s|$))", title) is not None


def _section_field(title: str, *, numbered: bool) -> str | None:
    number = _NUMBER.match(title)
    lowered = (title[number.end():] if number else title).lower()
    # A finding or historical narrative can mention checks without being the
    # report's checks section. Only a heading that names the field qualifies.
    leading = re.sub(r"^(?:(?:주요|직접|실행한|수행한|관련|최종|추가|알려진|known|executed|performed|focused|relevant)\s+)+", "", lowered)
    if not any(leading.startswith(word) and _has_word(leading, word)
               for _field, _numbers, words in _SECTION_FIELDS for word in words):
        return None
    matches = [field for field, _numbers, words in _SECTION_FIELDS
               if any(_has_word(lowered, word) for word in words)]
    if matches and set(matches) <= {"remaining", "limits", "follow_up"}:
        # "미검증·한계", "남은 위험", "남은 것·후속": a failure heading word wins, then a limit or risk.
        if any(_has_word(lowered, word) for word in _FAILURE_HEADINGS):
            return "remaining"
        if "limits" in matches or re.search(r"위험|\brisks?\b", lowered):
            return "limits"
        return "remaining" if "remaining" in matches else "follow_up"
    return matches[0] if len(matches) == 1 else None


def report_sections(text: str, *, numbered: bool = False) -> dict[str, list[str]]:
    """The report's own lines under its overview, verification and remaining-work headings.

    `numbered` is accepted for older callers, but ordinals never decide meaning.
    Mixed/unknown headings and code fences cannot populate a guessed field."""
    sections: dict[str, list[str]] = {}
    current = None
    fence = None
    for raw in (text or "").splitlines():
        if raw.lstrip().startswith(('```', '~~~')):
            marker = raw.lstrip()[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence:
            continue
        match = _SECTION.match(raw)
        if match:
            current = _section_field(match["title"].strip(), numbered=numbered)
            continue
        bold = _BOLD_FIELD.match(raw)
        if bold:
            label = bold["title"].strip().rstrip(":：")
            current = _section_field(label, numbered=numbered)
            value = bold["value"].strip()
            if current and value:
                sections.setdefault(current, []).append(value)
            continue
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", raw).strip()
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            normalized_cells = [re.sub(r"[*:]", "", cell).strip().casefold() for cell in cells]
            if len(normalized_cells) >= 2 and normalized_cells[:2] in (["검사", "결과"], ["check", "result"], ["item", "result"]):
                continue
            label = re.sub(r"[*:]", "", cells[0]).strip() if cells else ""
            field = _section_field(label, numbered=numbered)
            if field:
                value = " · ".join(cell for cell in cells[1:] if cell and not re.fullmatch(r"[-: ]+", cell))
                if value:
                    sections.setdefault(field, []).append(value)
                current = None
                continue
        if (current and line and not re.fullmatch(r"[|:\-\s]+", line)
                and not any(marker in line for marker in _CARRIER_MARKERS)):
            # A verification section can explicitly say a check was not run.
            # Preserve that declaration as unverified; never call it a pass.
            field = "remaining" if current == "verified" and _UNRUN_CHECK.search(line) else current
            sections.setdefault(field, []).append(line.strip("|").strip())
    return sections


def report_verdict(text: str) -> str:
    """The report's first non-heading line when it states the verdict, else ""."""
    fence = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence or not line or line.startswith("#"):
            continue
        match = _VERDICT_LINE.match(line.replace("**", ""))
        return _line(match["verdict"].strip("* "), 120) if match else ""
    return ""


def _publication_lines(text: str):
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", raw).strip()
        if (line and not line.startswith(("#", "|", "```")) and not _VERDICT_LINE.match(line.replace("**", ""))
                and _PUBLICATION_LINE.search(line) and _PUBLICATION_WORDS.search(line)):
            yield line


def is_publication_line(line: str) -> bool:
    return bool(_PUBLICATION_LINE.search(line or "") and _PUBLICATION_WORDS.search(line or ""))


def report_publication(text: str) -> str:
    """``#<n> · head <sha12> · CI <state> · <mergeability>`` from the report's PR line, else ""."""
    for line in _publication_lines(text):
        plain = line.replace("`", "")
        found = _PUBLICATION_LINE.search(plain)
        parts = ["#" + (found["url"] or found["number"])]
        for pattern in _PUBLICATION_FIELDS:
            value = pattern.search(plain)
            if value:
                word = value["value"].strip()
                parts.append(("head " + word[:12]) if pattern is _PUBLICATION_FIELDS[0]
                             else ("CI " + word) if pattern is _PUBLICATION_FIELDS[1] else word)
        return _line(" · ".join(parts), 120)
    return ""


def _joined(lines, cap=MAX_FIELD_CHARS) -> str:
    return _line(" · ".join(dict.fromkeys(item for item in lines if item)), cap)


_COMMAND_LINE = re.compile(
    r"^(?:[$>]\s*|`|(?:python3?|pytest|uv|git|make|npm|npx|cargo|go)(?=\s|$)|\.?/?tools/"
    r"|[\w./-]+\.(?:test\.py|test\.sh|py|sh)\b)", re.I)
_COMMAND_RESULT = re.compile(r"\b(?:pass(?:ed)?|fail(?:ed)?(?![-_][a-z])|통과|실패)\b", re.I)
# A failure signal that is not a pass/fail word: it must never read as "0 failures".
_ERROR_WORD = re.compile(r"error|exception|traceback|오류|에러|예외", re.I)
# "no error", "오류 없음", "실패한 단계 없음", "FAIL=0", "errors: 0", "0 failed" say nothing went
# wrong; strip them before looking for failure words.
_NEGATED_FAILURE = re.compile(
    r"\b(?:no|without|zero|0|nothing|none)\s+(?:new\s+|test\s+|unit\s+)?"
    r"(?:errors?|failures?|failed|failing|fails?|exceptions?)\b"
    r"|\b(?:errors?|failures?|failed|fails?|exceptions?)\s*[=:：]?\s*0(?![\w가-힣]|[.,]\d)"
    r"|(?:오류|에러|실패|예외)\s*[=:：]?\s*0\s*(?:건|개)?(?![\w가-힣]|[.,]\d)"
    r"|(?:오류|에러|실패|예외)(?:한|된|난|가|이|은|는|도)?\s*(?:[가-힣]{1,8}\s+)?없[가-힣]*"
    r"|(?:오류|에러|실패)(?:가|는|이|도)?\s*아니[가-힣]*|\bnot an? (?:failure|error)\b"
    r"|\berror-?free\b|깨지지\s*않[가-힣]*",
    re.I)


def _unnegated(text: str) -> str:
    return _NEGATED_FAILURE.sub(" ", text)


def _command_failures(line) -> int:
    """Fail words on one command row; an error word alone counts once, never as zero."""
    rest = _unnegated(line)
    words = [match.group(0).lower() for match in _COMMAND_RESULT.finditer(rest)]
    failed = sum(value in {"fail", "failed", "실패"} for value in words)
    return failed or (1 if _ERROR_WORD.search(rest) else 0)


def _verified_summary(lines, t):
    """Keep commands intact in the record; summarize bare command rows for people.

    Only lines that start like a command are folded; every other line stays as written.
    A folded group with no pass/fail word says so instead of reading as zero failures.
    """
    commands = [line for line in lines if _COMMAND_LINE.search(line.strip())]
    prose = [line for line in lines if line not in commands]
    if not commands:
        return _joined(lines) or t("completion.no_checks")
    passed = failures = 0
    for line in commands:
        words = [match.group(0).lower() for match in _COMMAND_RESULT.finditer(_unnegated(line))]
        failures += _command_failures(line)
        passed += sum(value in {"pass", "passed", "통과"} for value in words)
    if not passed and not failures:
        summary = t("completion.command_checks_unmarked", count=len(commands))
    else:
        summary = t("completion.command_checks", count=len(commands), passed=passed, failed=failures)
    return _joined([*prose, summary])


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


_NONE_ITEMS = {"없음", "해당 없음", "없습니다", "none", "n/a", "not applicable"}
_NONE_LEAD = re.compile(r"^(?:없음|해당 없음|없습니다|none|n/a|not applicable)(?=$|[\s.:：,;()（）—–-])", re.I)
# What a failed/skipped, unverified or remaining-work line says went wrong or was not done.
_FAILURE_TEXT = re.compile(
    r"실패|오류|에러|깨[짐지진]|쓰면 안|사용할 수 없|못\s?했|하지 못|지 못|미실행|미확인|미검증|미관측|중단"
    r"|돌리지 않|실행하지 않|건너뛰|건너뛴|건너뜀"
    r"|\bfail(?:s|ed|ing|ures?)?\b(?![-_][a-z])|\berrors?\b|\bnot (?:re-?)?run\b|\bunrun\b|\bskipped\b"
    r"|\bunverified\b|\bunavailable\b|\bblocked\b|not usable", re.I)
# A failure word in a fixed-past, conditional, possible or pre-existing context: the line alone
# cannot say it failed now, so the result points at the report instead of asserting either way.
_UNSURE_FAILURE = re.compile(
    r"고쳤|고침|수정했|수정함|해결했|해결함|해결됨|해결 완료|교정|닫았|닫음|닫혔|닫힘|복구했"
    r"|재시도\s*후|재실행\s*후|재실행에서|재시도에서|재시험에서|단독으로 돌리면|단독 재실행|후\s*통과"
    r"|(?:하|되|않으|못하|다르|나)면(?=[\s,.]|$)|(?:실패|오류|에러)\s*(?:시|때)(?![가-힣])|때는|할 때|될 때|경우"
    r"|수\s*있|가능성|baseline|기준선|기준에서도|기준 트리|base에서도|기존에도|기존부터|기존 실패|이전부터|원래부터|원래 있던"
    r"|무관|관계\s*없|상관\s*없|main에서도|main에도|새 실패 아|회귀가 아|회귀 아님|간헐|flaky|flake|재현되지 않|재현 불가|일회성"
    r"|필요\s*없|불필요|범위 밖|비범위|의도적으로"
    r"|\b(?:fixed|resolved|addressed|corrected|closed|recovered|if|when|may|might|could|possibly"
    r"|pre-?existing|unrelated|known[- ]fail\w*|not a regression|not needed|unnecessary|out of scope|by design)\b",
    re.I)
# Stated as still broken: a context word above does not soften it.
_STILL_FAILING = re.compile(r"못\s?했|미해결|여전히|\b(?:still (?:fail\w*|broken)|unresolved|not (?:yet )?fixed)\b", re.I)
_DECISION_WORDS = ("사용자가 정해야", "사용자 결정", "결정 후", "답을 기다", "must decide", "decision needed")


def _is_none_item(item) -> bool:
    text = str(item or "").strip()
    return not text or re.sub(r"[.!。 ]+$", "", text).casefold() in _NONE_ITEMS


def _failure_kind(line, *, verified=False) -> str | None:
    """``failure``, ``check`` (cannot tell, or something left without a failure word) or None.

    A verification line counts only as ``check``: a command row its folded count calls failed, or
    a prose line the remaining-work reading calls a failure. Whether it is this task's, an earlier
    round's or a baseline's is the report's to say."""
    text = str(line or "").strip()
    if _is_none_item(text):
        return None
    rest = _unnegated(text)
    if verified:
        if _COMMAND_LINE.search(text):
            return "check" if _command_failures(text) else None
        return "check" if _failure_kind(_UNRUN_WORD.sub(" ", text)) == "failure" else None
    if not _FAILURE_TEXT.search(rest):
        if rest != text and not re.search(r"[\w가-힣]", rest):
            return None  # "실패한 단계 없음 (errors: 0)": only a statement that nothing failed
        if _NONE_LEAD.match(text):
            return None  # "없음 — 모든 단계 수행"
        return "check"
    if _UNSURE_FAILURE.search(rest) and not _STILL_FAILING.search(rest):
        return "check"
    return "failure"


def _result_line(route, t, remaining=(), *, limits=(), follow_up=(), verified=()) -> str:
    """Name a failure only from the failed/skipped, unverified and remaining-work lines.

    Follow-ups never make a finished task read as failed. A line that cannot be told apart (a
    fixed past failure, a possible one), a limit or risk, and a failed check row or verification
    line point at the report instead of asserting a failure or a clean close."""
    kinds = ({_failure_kind(item) for item in remaining}
             | {"check" for item in limits if _failure_kind(item)}
             | {_failure_kind(line, verified=True) for line in verified})
    has_failure = "failure" in kinds
    has_decision = any(word in str(item).casefold() for item in (*remaining, *limits, *follow_up)
                       if not _is_none_item(item) for word in _DECISION_WORDS)
    note = (t("completion.result_check") if "check" in kinds
            else t("completion.result_follow_up") if any(not _is_none_item(item) for item in follow_up)
            else "")
    plan = route.get("route_plan")
    if isinstance(plan, dict):
        try:
            record = json.loads((Path(route["artifact_root"]) / plan["decision"]).read_text(encoding="utf-8"))
            legs = len(record["decision"]["proposal"]["legs"])
            index = int(plan["index"])
        except (OSError, ValueError, KeyError, TypeError):
            legs = index = None
        if legs is not None and index + 1 < legs:
            result = t("completion.result_leg", index=index + 1, legs=legs)
            if has_failure:
                return result + " · " + t("completion.result_with_remaining")
            if has_decision:
                return result + " · " + t("completion.result_decision")
            return result + " · " + note if note else result
    if has_failure:
        return t("completion.result_with_remaining")
    if has_decision:
        return t("completion.result_decision")
    if "check" in kinds:
        return t("completion.result_done") + " — " + note
    return t("completion.result_done") + " · " + note if note else t("completion.result_done")


def _elapsed_text(seconds) -> str | None:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        return None
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {sec:02d}s" if minutes else f"{sec}s"


def cost_line(cost, t) -> str:
    """Elapsed time and session tokens, each `unknown` when not observed; never a share of a plan.

    `cost` holds `elapsed_seconds`, `tokens` (the sum over the sessions that reported one),
    `sessions` (attempts counted), `unknown` (how many reported none) and `partial` (how many
    reported only some of their turns, which makes the sum a lower bound)."""
    cost = cost if isinstance(cost, dict) else {}
    count = lambda key, default=0: cost.get(key) if isinstance(cost.get(key), int) else default
    elapsed = _elapsed_text(cost.get("elapsed_seconds")) or t("completion.cost_unknown")
    sessions = count("sessions")
    unknown, partial = count("unknown", sessions), count("partial")
    tokens = cost.get("tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or sessions <= unknown:
        return t("completion.cost", elapsed=elapsed, tokens=t("completion.cost_tokens_unknown"))
    detail = [t("completion.cost_detail_all", sessions=sessions) if not unknown
              else t("completion.cost_detail_some", known=sessions - unknown, sessions=sessions)]
    if unknown:
        detail.append(t("completion.cost_detail_unknown", count=unknown))
    if partial:
        detail.append(t("completion.cost_detail_partial", count=partial))
    words = t("completion.cost_tokens_at_least" if partial else "completion.cost_tokens",
              tokens=f"{tokens:,}", detail=", ".join(detail))
    return t("completion.cost", elapsed=elapsed, tokens=words)


def _entrance_line(route, primary, t):
    """The one-time "the results list is here" line for the first user-facing cycle.

    Read-only: `artifact_entrance` writes `.runtime/artifact-entrance.json` once
    when a root first holds a user-facing document.  A missing file, an
    unreadable one, or a report outside that first cycle's folder adds no line.
    """
    try:
        root = route.get("artifact_root") if isinstance(route, dict) else None
        if not isinstance(root, str) or not root or not primary:
            return None
        record = json.loads((Path(root) / ".runtime" / "artifact-entrance.json")
                            .read_text(encoding="utf-8"))
        first_path = record.get("first_cycle_path") if isinstance(record, dict) else None
        if not isinstance(first_path, str) or not first_path:
            return None
        if not Path(primary).resolve().is_relative_to((Path(root) / first_path).resolve()):
            return None
    except (OSError, ValueError, TypeError, UnicodeError):
        return None
    return t("completion.entrance", path=str(Path(root) / "README.md"))


def completion_summary(route, primary, *, route_file=None, t=None, cost=None, corrections=None, assessment=None,
                       publication=None) -> str:
    """The runtime's close text for one completed owner: intent, result, cost, changes, checks, the rest.

    Every value comes from the sealed route, the route outcome, the registered
    attempts (`cost`, when the caller observed them) and the primary report
    itself; a report whose headings cannot be matched is named, not summarised,
    so nothing is invented and nothing unverified reads as verified. The labels
    come from the shared UI-text catalog (`t`; English unless Korean is declared).
    A shared-spec `publication` the settlement observed as pending is named on
    one line right after the result: it does not hold the completion back.
    """
    t = t or ui_translator()
    request = route.get("work_request") if isinstance(route.get("work_request"), dict) else {}
    intent = route_intent(route) or {}
    head = f"{t('completion.head')} {route.get('capability') or '-'} · route {route.get('route_id') or '-'}"
    lines = [head]
    goal = intent.get("intent") or ""
    if not goal and request.get("text"):
        goal = draft(request["text"]).get("intent") or ""
    if goal:
        # The supervisor wrote this intent; a short reminder is enough to match the result.
        original = (t("completion.intent_original", original=_line(intent["original"], 60))
                    if intent.get("original") else "")
        lines.append(t("completion.intent", intent=_line(goal, 120), original=original))
    try:
        text = Path(primary).read_text(encoding="utf-8") if primary else ""
    except (OSError, UnicodeError):
        text = ""
    code_report = route.get("capability") == "autopilot-code" or Path(str(primary or "")).name == "final_report.md"
    sections = report_sections(text, numbered=code_report)
    result = _result_line(route, t, sections.get("remaining") or [], limits=sections.get("limits") or [],
                          follow_up=sections.get("follow_up") or [], verified=sections.get("verified") or [])
    if assessment == "FAIL":
        findings = t("completion.result_audit_findings")
        result = findings if result == t("completion.result_done") else result + " · " + findings
    lines.append(t("completion.result", result=result))
    verdict = report_verdict(text)
    if verdict:
        lines.append(t("completion.verdict", verdict=verdict))
    if isinstance(publication, dict) and publication.get("status") == "pending":
        reason = publication.get("detail") or publication.get("reason") or "-"
        lines.append(t("completion.publication_pending", reason=_line(reason, 160)))
    for state in ("undelivered", "unknown"):
        if (corrections or {}).get(state):
            lines.append(t("completion.corrections_" + state, count=corrections[state]))
    if cost is not None:
        lines.append(cost_line(cost, t))
    degraded = _review_degraded(route_file)
    if sections:
        if sections.get("changed"):
            lines.append(t("completion.changed", items=_joined(sections["changed"])))
        checks = _verified_summary(sections.get("verified") or [], t)
        lines.append(t("completion.checks", items=checks))
        rest = [line for field in ("remaining", "limits", "follow_up") for line in sections.get(field) or []
                if not is_publication_line(line)]
        lines.append(t("completion.remaining", items=_joined(rest) or t("completion.no_remaining")))
    else:
        lines.append(t("completion.unreadable"))
    if degraded:
        lines.append(t("completion.degraded", nodes=", ".join(degraded)))
    published = report_publication(text)
    if published:
        lines.append(t("completion.publication", line=published))
    prs = [] if published else list(dict.fromkeys(_PR_URL.findall(text)))[:3]
    entrance = _entrance_line(route, primary, t)
    if entrance:
        lines.append(entrance)
    artifact = t("completion.artifact", path=primary) if primary else t("completion.no_artifact")
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
    if not text.startswith(summary_heads()) or "\n\n" in text or any(m in text for m in _CARRIER_MARKERS):
        return ""
    return text


# ---------------------------------------------------------------------------
# A direction that crosses the intent's "not doing" is the person's to choose
# ---------------------------------------------------------------------------

_PARTICLES = ("으로", "에서", "에게", "까지", "부터", "하고", "이나", "을", "를", "은", "는", "이", "가", "의",
              "에", "로", "도", "만", "와", "과")
_STOPWORDS = frozenset(("하지", "않기", "않음", "않는다", "말기", "금지", "없이", "추가", "새로", "새로운",
                        "the", "and", "any", "not", "new", "add", "adding", "without", "don't", "never"))
# A proposal names parts in English; a "not doing" line is often Korean. Whole words only.
_SYNONYMS = {"deploy": "배포", "push": "푸시", "merge": "병합", "delete": "삭제", "release": "릴리스",
             "publish": "출판", "migration": "마이그레이션", "install": "설치"}
# A clause that says the work keeps away from something, or leaves it to someone else, names it
# without doing it ("새 게이트를 추가하지 않는다", "병합·설치는 79에게 인계").
_KEEPS_AWAY = re.compile(r"않|없|아닌|아니|금지|유지|보존|인계|넘기|넘김|넘긴|넘길|넘겨|맡기|맡김|맡긴|맡겨|그대로"
                         r"|부모|메인 세션"
                         r"|\b(?:without|no|not|never|keep|keeps|kept|unchanged|leave|leaves|hand(?:s|ed)?\s*(?:off|over)"
                         r"|handoff|handover|parent|main session)\b|n't\b", re.I)
# Clauses end at `;`, a sentence end or a parenthesis; `·`, `,`, `/` and "and" join one clause's list.
_CLAUSE = re.compile(r";|\.(?:\s|$)|(?<=[다요])\s|[()（）]")
_ITEM_PARTS = re.compile(r"\s*[·,/、，]\s*")


def _content_words(text: str) -> list[str]:
    words = []
    for raw in re.findall(r"[0-9A-Za-z가-힣_-]+", (text or "").lower()):
        word = raw.strip("_-")
        if not word.isascii():
            for particle in _PARTICLES:
                if len(word) > len(particle) + 1 and word.endswith(particle):
                    word = word[:-len(particle)]
                    break
        if word in _STOPWORDS:
            continue
        if len(word) >= 2:
            words.append(word)
    return words


def _direction_words(proposal) -> set[str]:
    """The words a proposal uses to say what it will do: its summary and each leg's parts.

    `why`, `done_when`, `verify`, `parallel`, `cwd` and the JSON keys are left out -- that is
    where a frame restates the limits it keeps -- and so is any clause that keeps away from
    something or hands it to someone else."""
    texts = [str(proposal.get("summary") or "")]
    for leg in proposal.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        texts += [str(leg.get(key) or "") for key in ("capability", "mode")]
        for key in ("graph", "extra_stages"):
            for part in leg.get(key) or []:
                texts.append(json.dumps(part, ensure_ascii=False) if isinstance(part, dict) else str(part))
    words: set[str] = set()
    for text in texts:
        for clause in _CLAUSE.split(text):
            if clause and not _KEEPS_AWAY.search(clause):
                words.update(_content_words(clause))
    words.update(korean for english, korean in _SYNONYMS.items() if english in words)
    return words


def crossed_not_doing(not_doing, proposal) -> list[str]:
    """The `안 할 것` items a proposal says it will do; [] when none or unreadable.

    An item is split on `·`, `,` and `/`; it is crossed when every word of one of its parts is a
    word of the proposal's direction (`_direction_words`). Mechanical: a different word for the
    same act passes, and the `[방향 통지]` summary is what lets the person correct that."""
    if not isinstance(proposal, dict):
        return []
    words = _direction_words(proposal)
    crossed = []
    for item in not_doing or []:
        for part in _ITEM_PARTS.split(item):
            needed = _content_words(part)
            if needed and all(word in words for word in needed):
                crossed.append(item)
                break
    return crossed


def supervisor_crossing(route, interview, answers) -> list[str]:
    """The `안 할 것` items a non-person answer would cross by choosing its route proposal.

    A supervisor answer (and the runtime's automatic agreement, which uses that authority) may not
    choose such a direction; the person answers that question. A person's own answer is never
    limited here."""
    import frame_interview
    if not isinstance(answers, dict) or answers.get("actor_kind") == "user":
        return []
    choice = frame_interview.route_choice(interview, answers)
    if not choice or choice.get("state") != "selected":
        return []
    request = route.get("work_request") if isinstance(route, dict) else None
    not_doing = draft(request.get("text") or "").get("not_doing") if isinstance(request, dict) else None
    return crossed_not_doing(not_doing, choice.get("proposal"))
